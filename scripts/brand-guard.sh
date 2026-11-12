#!/usr/bin/env bash
# brand-guard: fail if user-visible Element branding grows beyond the baseline.
# Patterns: element.io, riot.im, app.element.*  (any tracked text file), and the
# product name "Element" (word, not io.element.* / element-hq) in string-resource files.
# Allowlist (.brand-guard-allowlist): one entry per line, '#' comments.
#   <path><TAB><max-lines>   baseline: file may contain up to N matching lines
#   glob <pathspec>          file(s) fully exempt (git pathspec glob)
# Usage: scripts/brand-guard.sh [--update]   (--update regenerates the baseline counts)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
AL=.brand-guard-allowlist
DOMAIN_RE='element\.io|riot\.im|app\.element\.'
NAME_RE='(^|[^A-Za-z0-9_.-])Element([^A-Za-z0-9_-]|$)'
RES_GLOBS=('*.strings' '*.stringsdict' '*.xcstrings')
EXCL=(':!scripts/brand-guard.sh' ":!$AL" ':!*.lock' ':!pnpm-lock.yaml' ':!Package.resolved' ':!CHANGES.md' ':!CHANGELOG.md' ':!LICENSE*' ':!AUTHORS*')

scan() { # prints "count<TAB>path"
  { git grep -c -I -E "$DOMAIN_RE" -- . "${EXCL[@]}" || true
    git grep -c -I -E "$NAME_RE" -- "${RES_GLOBS[@]}" "${EXCL[@]}" || true; } \
  | awk -F: '{c=$NF; sub(/:[0-9]+$/,""); n[$0]+=0; n[$0]+=c} END{for(p in n) printf "%d\t%s\n", n[p], p}' | sort -k2
}
# Note: a line matching both rules in a resource file is counted twice (stable, so fine for a baseline).

if [ "${1:-}" = "--update" ]; then
  { sed -n '1,/^# ---baseline---$/p' "$AL" 2>/dev/null || echo '# ---baseline---'; } > "$AL.tmp"
  grep -q '^# ---baseline---$' "$AL.tmp" || echo '# ---baseline---' >> "$AL.tmp"
  scan | awk -F'\t' '{printf "%s\t%d\n",$2,$1}' >> "$AL.tmp"
  mv "$AL.tmp" "$AL"; echo "baseline updated"; exit 0
fi

[ -f "$AL" ] || { echo "missing $AL" >&2; exit 2; }
globs=(); declare -A max=()
while IFS= read -r line; do
  case "$line" in ''|'#'*) continue;; glob\ *) globs+=(":!${line#glob }");; *) max["${line%%$'\t'*}"]="${line##*$'\t'}";; esac
done < "$AL"
fail=0
while IFS=$'\t' read -r cnt path; do
  [ -n "$path" ] || continue
  if [ "${#globs[@]}" -gt 0 ] && [ -z "$(git ls-files -- . "${globs[@]}" | grep -Fx -- "$path" || true)" ]; then continue; fi
  allowed="${max[$path]:-0}"
  if [ "$cnt" -gt "$allowed" ]; then
    echo "BRAND-GUARD: $path has $cnt Element-branding matches (allowed $allowed):" >&2
    git grep -n -I -E "$DOMAIN_RE" -- "$path" >&2 || true
    git grep -n -I -E "$NAME_RE" -- "$path" >&2 || true
    fail=1
  fi
done < <(scan)
[ "$fail" = 0 ] && echo "brand-guard: OK" || { echo "brand-guard: FAILED. Remove the branding, or (if intentional) run scripts/brand-guard.sh --update" >&2; exit 1; }
