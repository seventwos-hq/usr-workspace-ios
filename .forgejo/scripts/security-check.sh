#!/usr/bin/env bash
# Security checks for Forgejo CI. Usage: security-check.sh gitleaks|deps
# Env: EVENT (push|pull_request), BASE_SHA, HEAD_SHA, DEFAULT_BRANCH
# Tools are pinned and checksum-verified; no curl|sh. Secret values are redacted.
set -euo pipefail

GITLEAKS_VERSION=8.28.0
GITLEAKS_SHA256=a65b5253807a68ac0cafa4414031fd740aeb55f54fb7e55f386acb52e6a840eb
OSV_VERSION=2.2.2
OSV_SHA256=c799274b682e0437c802d4714a136ad09b39c65547c460049ba5358d28afa576
DENY_VERSION=0.20.2
DENY_SHA256=9f12ed4c49936e09b48bf862b595cde2fe64fcbd9d74dfacac6131ca824c8d5f

TOOLS="${RUNNER_TEMP:-/tmp}/sec-tools"
mkdir -p "$TOOLS"
ZERO=0000000000000000000000000000000000000000

fetch() { # url sha256 out
  curl -fsSL --retry 3 -o "$3" "$1"
  echo "$2  $3" | sha256sum -c - >/dev/null || { echo "checksum mismatch for $1"; exit 2; }
}

# Range of commits this run is responsible for, as git-log arguments.
range_args() {
  local def="origin/${DEFAULT_BRANCH:-main}"
  if [ "$EVENT" = "pull_request" ]; then
    echo "${BASE_SHA}..${HEAD_SHA}"
  elif [ -n "${BASE_SHA:-}" ] && [ "$BASE_SHA" != "$ZERO" ] && git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then
    echo "${BASE_SHA}..${HEAD_SHA}"
  else
    echo "${HEAD_SHA} --not ${def}"
  fi
}

# Base commit to diff the head against (for dependency checks).
base_commit() {
  local def="origin/${DEFAULT_BRANCH:-main}"
  if [ "$EVENT" = "pull_request" ]; then echo "$BASE_SHA"
  elif [ -n "${BASE_SHA:-}" ] && [ "$BASE_SHA" != "$ZERO" ] && git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then echo "$BASE_SHA"
  else git merge-base "$HEAD_SHA" "$def" 2>/dev/null || true; fi
}

run_gitleaks() {
  fetch "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" "$GITLEAKS_SHA256" "$TOOLS/gl.tgz"
  tar -xzf "$TOOLS/gl.tgz" -C "$TOOLS" gitleaks
  local r; r="$(range_args)"
  echo "gitleaks range: $r"
  local cfg=()
  [ -f .gitleaks.toml ] && cfg=(--config .gitleaks.toml)
  # shellcheck disable=SC2086
  "$TOOLS/gitleaks" git "${cfg[@]}" --log-opts="$r" --redact --exit-code 1 --no-banner .
}

LOCK_RE='(^|/)(Cargo\.lock|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|Package\.resolved|gradle\.lockfile|deny\.toml)$'

run_deps() {
  local base; base="$(base_commit)"
  if [ -z "$base" ]; then echo "no base commit found; scanning nothing"; exit 0; fi
  local changed; changed="$(git diff --name-only "$base" "$HEAD_SHA" | grep -E "$LOCK_RE" || true)"
  if [ -z "$changed" ]; then echo "no lockfile or deny.toml changed; skipping"; exit 0; fi
  echo "changed dependency files:"; echo "$changed"

  local lockfiles; lockfiles="$(echo "$changed" | grep -v 'deny\.toml$' || true)"
  local rc=0
  if [ -n "$lockfiles" ]; then
    fetch "https://github.com/google/osv-scanner/releases/download/v${OSV_VERSION}/osv-scanner_linux_amd64" "$OSV_SHA256" "$TOOLS/osv-scanner"
    chmod +x "$TOOLS/osv-scanner"
    osv_diff "$base" "$lockfiles" || rc=1
  fi
  if echo "$changed" | grep -qE '(^|/)(Cargo\.lock|deny\.toml)$' && [ -f deny.toml ]; then
    deny_diff "$base" || rc=1
  fi
  exit $rc
}

osv_scan() { # dir lockfile-relpath out.json
  (cd "$1" && "$TOOLS/osv-scanner" scan source --format json -L "$2" > "$3" 2>/dev/null) || true
  [ -s "$3" ] || echo '{"results":[]}' > "$3"
}

osv_ids() { # json -> "lockfile|pkg|id" lines
  python3 - "$1" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
for r in d.get("results") or []:
    src=r["source"]["path"].split("/")[-1]
    for p in r.get("packages") or []:
        n=p["package"]["name"]
        for v in p.get("vulnerabilities") or []:
            print(f"{src}|{n}|{v['id']}")
PY
}

osv_diff() {
  local base="$1" files="$2" w; w="$(mktemp -d)"
  local bad=0 f
  while IFS= read -r f; do
    [ -f "$f" ] || continue   # lockfile deleted
    mkdir -p "$w/head/$(dirname "$f")" "$w/base/$(dirname "$f")"
    cp "$f" "$w/head/$f"
    git show "${base}:${f}" > "$w/base/$f" 2>/dev/null || rm -f "$w/base/$f"
    osv_scan "$w/head" "$f" "$w/h.json"
    if [ -f "$w/base/$f" ]; then osv_scan "$w/base" "$f" "$w/b.json"; else echo '{"results":[]}' > "$w/b.json"; fi
    osv_ids "$w/h.json" | sort -u > "$w/h.txt"; osv_ids "$w/b.json" | sort -u > "$w/b.txt"
    local new; new="$(comm -13 "$w/b.txt" "$w/h.txt" || true)"
    if [ -n "$new" ]; then echo "NEW vulnerabilities introduced in $f:"; echo "$new"; bad=1
    else echo "osv-scanner: no new advisories in $f (head $(wc -l < "$w/h.txt"), base $(wc -l < "$w/b.txt"))"; fi
  done <<< "$files"
  return $bad
}

deny_findings() { # dir -> fingerprint lines
  (cd "$1" && "$TOOLS/cargo-deny" --format json check advisories bans sources 2>&1 >/dev/null || true) | python3 -c '
import sys,json
for l in sys.stdin:
    try: d=json.loads(l)
    except Exception: continue
    f=d.get("fields",{})
    if d.get("type")=="diagnostic" and f.get("severity")=="error":
        print(f.get("code","?")+"|"+f.get("message","?"))
' | sort -u
}

deny_diff() {
  local base="$1" w; w="$(mktemp -d)"
  fetch "https://github.com/EmbarkStudios/cargo-deny/releases/download/${DENY_VERSION}/cargo-deny-${DENY_VERSION}-x86_64-unknown-linux-musl.tar.gz" "$DENY_SHA256" "$TOOLS/cd.tgz"
  tar -xzf "$TOOLS/cd.tgz" -C "$TOOLS" --strip-components=1 "cargo-deny-${DENY_VERSION}-x86_64-unknown-linux-musl/cargo-deny"
  git worktree add --detach "$w/base" "$base" >/dev/null
  deny_findings "$PWD" > "$w/h.txt"
  deny_findings "$w/base" > "$w/b.txt"
  git worktree remove --force "$w/base" || true
  local new; new="$(comm -13 "$w/b.txt" "$w/h.txt" || true)"
  if [ -n "$new" ]; then echo "NEW cargo-deny errors:"; echo "$new"; return 1; fi
  echo "cargo-deny: no new errors (head $(wc -l < "$w/h.txt"), base $(wc -l < "$w/b.txt"))"
}

case "${1:-}" in
  gitleaks) run_gitleaks ;;
  deps) run_deps ;;
  *) echo "usage: $0 gitleaks|deps"; exit 64 ;;
esac
