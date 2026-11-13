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

EMPTY_TREE=4b825dc642cb6eb9a060e54bf8d69288fbee4904

# True when the old tip is unusable and nothing but the default branch itself holds the new commits:
# then scan the reachable history instead of excluding the default branch (which would exclude everything).
unusable_default_push() {
  [ "$EVENT" = "pull_request" ] && return 1
  [ "${REF_NAME:-}" = "${DEFAULT_BRANCH:-main}" ] || return 1
  [ -n "${BASE_SHA:-}" ] && [ "$BASE_SHA" != "$ZERO" ] && git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null && return 1
  return 0
}

# Range of commits this run is responsible for, as git-log arguments.
range_args() {
  local def="origin/${DEFAULT_BRANCH:-main}"
  if [ "$EVENT" = "pull_request" ]; then
    echo "${BASE_SHA}..${HEAD_SHA}"
  elif [ -n "${BASE_SHA:-}" ] && [ "$BASE_SHA" != "$ZERO" ] && git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then
    echo "${BASE_SHA}..${HEAD_SHA}"
  elif unusable_default_push; then
    echo "${HEAD_SHA}"
  else
    echo "${HEAD_SHA} --not ${def}"
  fi
}

# Base to diff the head against (for dependency checks).
base_commit() {
  local def="origin/${DEFAULT_BRANCH:-main}"
  if [ "$EVENT" = "pull_request" ]; then echo "$BASE_SHA"
  elif [ -n "${BASE_SHA:-}" ] && [ "$BASE_SHA" != "$ZERO" ] && git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then echo "$BASE_SHA"
  elif unusable_default_push; then echo "$EMPTY_TREE"
  else git merge-base "$HEAD_SHA" "$def" 2>/dev/null || echo "$EMPTY_TREE"; fi
}

# Trusted policy ref: pre-push tip ONLY for a push to the default branch (protected); every other event/branch uses the
# fully qualified default-branch ref (a feature-branch "before" tip or a PR base can be attacker-controlled).
# On a default-branch push with no usable pre-push tip, the empty tree (built-in rules, no ignores).
trusted_ref() {
  if [ "$EVENT" != "pull_request" ] && [ "${REF_NAME:-}" = "${DEFAULT_BRANCH:-main}" ]; then
    if [ -n "${BASE_SHA:-}" ] && [ "$BASE_SHA" != "$ZERO" ] && git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then echo "$BASE_SHA"; else echo "$EMPTY_TREE"; fi
  else echo "refs/remotes/origin/${DEFAULT_BRANCH:-main}"; fi
}

run_gitleaks() {
  fetch "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" "$GITLEAKS_SHA256" "$TOOLS/gl.tgz"
  tar -xzf "$TOOLS/gl.tgz" -C "$TOOLS" gitleaks
  local r; r="$(range_args)"
  echo "gitleaks range: $r"
  # Trusted rules only: config and ignore file come from the protected default branch, never from the proposed tree or a prior branch tip.
  # (gitleaks would otherwise auto-load ./.gitleaks.toml / ./.gitleaksignore from the checkout.)
  # Trusted ref: pre-push tip ONLY for a push to the default branch (protected); every other event/branch uses the
  # default-branch tip (a feature-branch "before" tip is attacker-controlled). On a default-branch push with no usable
  # pre-push tip, fall back to built-in rules with no ignores (empty tree).
  local base cfgf ign; base="$(trusted_ref)"
  cfgf="$TOOLS/gitleaks-trusted.toml"; ign="$TOOLS/gitleaksignore-trusted"
  if ! git show "${base}:.gitleaks.toml" > "$cfgf" 2>/dev/null; then
    printf '[extend]\nuseDefault = true\n' > "$cfgf"
  fi
  git show "${base}:.gitleaksignore" > "$ign" 2>/dev/null || : > "$ign"
  # --diff-merges=first-parent: include patches for merge commits so secrets added only in conflict resolution are scanned.
  # shellcheck disable=SC2086
  "$TOOLS/gitleaks" git --config "$cfgf" --gitleaks-ignore-path "$ign" --ignore-gitleaks-allow \
    --log-opts="--diff-merges=first-parent $r" --redact --exit-code 1 --no-banner .
}

LOCK_RE='(^|/)(Cargo\.lock|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|Package\.resolved|gradle\.lockfile|deny\.toml)$'

run_deps() {
  local base; base="$(base_commit)"
  # NUL-delimited throughout: paths with non-ASCII/control/newline characters are neither quoted nor split.
  local alln="$TOOLS/diff-all.z" changed="$TOOLS/diff-deps.z" lockfiles="$TOOLS/diff-locks.z"
  git diff --name-only -z "$base" "$HEAD_SHA" > "$alln" || { echo "git diff failed (fail closed)"; exit 2; }
  grep -zE "$LOCK_RE" "$alln" > "$changed" || true
  if [ ! -s "$changed" ]; then echo "no lockfile or deny.toml changed; skipping"; exit 0; fi
  echo "changed dependency files:"; tr '\0' '\n' < "$changed"
  grep -zvE 'deny\.toml$' "$changed" > "$lockfiles" || true
  local rc=0
  if [ -s "$lockfiles" ]; then
    fetch "https://github.com/google/osv-scanner/releases/download/v${OSV_VERSION}/osv-scanner_linux_amd64" "$OSV_SHA256" "$TOOLS/osv-scanner"
    chmod +x "$TOOLS/osv-scanner"
    osv_diff "$base" "$lockfiles" || rc=$?
    [ "$rc" -le 1 ] || { echo "osv-scanner stage failed (exit $rc)"; exit "$rc"; }
  fi
  if grep -zqE '(^|/)(Cargo\.lock|deny\.toml)$' "$changed" && [ -f deny.toml ]; then
    local drc=0
    deny_diff "$base" || drc=$?
    [ "$drc" -le "$rc" ] || rc=$drc   # keep the worst code; never let a later pass mask an earlier error
  fi
  exit $rc
}

osv_scan() { # dir lockfile-relpath out.json [strict] ; strict (head): no-packages is a failure, not an empty scan; fails closed on operational errors
  local rc=0
  (cd "$1" && "$TOOLS/osv-scanner" scan source --format json -L "$2" > "$3") 2>"$3.err" || rc=$?
  case "$rc" in
    0|1) ;;                                   # 0 clean, 1 vulnerabilities found
    128) if [ "${4:-}" != strict ] && grep -qi 'no package sources found' "$3.err"; then echo '{"results":[]}' > "$3"; echo "warning: osv-scanner found no packages in $2"
         else echo "osv-scanner failed (exit 128) on $2:"; cat "$3.err"; return 2; fi ;;
    *) echo "osv-scanner failed (exit $rc) on $2:"; cat "$3.err"; return 2 ;;
  esac
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert isinstance(d.get("results"),list)' "$3" \
    || { echo "osv-scanner produced invalid JSON for $2"; return 2; }
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
  local base="$1" files="$2" w bad=0 f new
  w="$(mktemp -d "$TOOLS/osv.XXXXXX")"
  while IFS= read -r -d '' f; do
    [ -f "$f" ] || continue   # lockfile deleted
    mkdir -p "$w/head/$(dirname "$f")" "$w/base/$(dirname "$f")"
    cp "$f" "$w/head/$f"
    git show "${base}:${f}" > "$w/base/$f" 2>/dev/null || rm -f "$w/base/$f"
    osv_scan "$w/head" "$f" "$w/h.json" strict || { rm -rf "$w"; return 2; }
    if [ -s "$w/base/$f" ]; then osv_scan "$w/base" "$f" "$w/b.json" || { rm -rf "$w"; return 2; }; else echo '{"results":[]}' > "$w/b.json"; fi
    osv_ids "$w/h.json" > "$w/h.raw" || { echo "failed to parse osv-scanner output for $f"; rm -rf "$w"; return 2; }
    osv_ids "$w/b.json" > "$w/b.raw" || { echo "failed to parse osv-scanner baseline output for $f"; rm -rf "$w"; return 2; }
    sort -u "$w/h.raw" > "$w/h.txt" && sort -u "$w/b.raw" > "$w/b.txt" || { echo "sort failed"; rm -rf "$w"; return 2; }
    new="$(comm -13 "$w/b.txt" "$w/h.txt")" || { echo "comm failed"; rm -rf "$w"; return 2; }
    if [ -n "$new" ]; then echo "NEW vulnerabilities introduced in $f:"; echo "$new"; bad=1
    else echo "osv-scanner: no new advisories in $f (head $(wc -l < "$w/h.txt"), base $(wc -l < "$w/b.txt"))"; fi
  done < "$files"
  rm -rf "$w"
  return $bad
}

deny_findings() { # dir out ; returns 2 when cargo-deny did not complete (no summary = operational failure)
  local d="$1" out="$2" raw drc=0
  raw="$(mktemp "$TOOLS/deny.XXXXXX")"
  # cargo-deny exits non-zero when it reports errors; completion is judged by the JSON summary below
  (cd "$d" && "$TOOLS/cargo-deny" --format json check --config "$DENY_CFG" $DENY_CHECKS) > /dev/null 2> "$raw" || drc=$?
  case "$drc" in 0|1) ;; *) echo "cargo-deny failed (exit $drc):"; tail -n 20 "$raw"; rm -f "$raw"; return 2 ;; esac
  python3 - "$raw" "$drc" > "$out" <<'PY' || { echo "cargo-deny did not complete cleanly (exit $drc):"; tail -n 20 "$raw"; rm -f "$raw"; return 2; }
import sys,json
summary=False; nerr=0; bad=False
for l in open(sys.argv[1]):
    try: d=json.loads(l)
    except Exception: continue
    if d.get("type")=="summary": summary=True
    f=d.get("fields",{})
    if d.get("type")!="diagnostic" and str(d.get("level","")).upper()=="ERROR":
        sys.stderr.write("cargo-deny logged an error: "+str(f.get("message",""))+"\n"); bad=True
    if d.get("type")=="diagnostic" and f.get("severity")=="error":
        ks=set()
        def walk(o):
            if isinstance(o,dict):
                k=o.get("Krate")
                if isinstance(k,dict) and "name" in k: ks.add(k["name"]+"@"+str(k.get("version","?")))
                for v in o.values(): walk(v)
            elif isinstance(o,list):
                for v in o: walk(v)
        walk(f.get("graphs"))
        adv=(f.get("advisory") or {}).get("id","")
        pk=",".join(sorted(ks)) or "?"
        nerr+=1
        print("|".join([f.get("code","?"),pk,adv,f.get("message","?")]))
if not summary:
    sys.stderr.write("cargo-deny produced no summary (did not complete)\n"); sys.exit(2)
if bad or (sys.argv[2]=="1" and nerr==0):
    sys.stderr.write("cargo-deny failed without reporting error diagnostics\n"); sys.exit(2)
PY
  sort -u -o "$out" "$out" || return 2
  rm -f "$raw"
}

deny_diff() {
  local base="$1" w new
  w="$(mktemp -d "$TOOLS/deny-diff.XXXXXX")"
  fetch "https://github.com/EmbarkStudios/cargo-deny/releases/download/${DENY_VERSION}/cargo-deny-${DENY_VERSION}-x86_64-unknown-linux-musl.tar.gz" "$DENY_SHA256" "$TOOLS/cd.tgz"
  tar -xzf "$TOOLS/cd.tgz" -C "$TOOLS" --strip-components=1 "cargo-deny-${DENY_VERSION}-x86_64-unknown-linux-musl/cargo-deny"
  # Policy (ignores, bans, sources) comes from the trusted ref, never from the proposed tree: the PR's own deny.toml
  # is never used. When the trusted ref has no deny.toml, run advisories only with an empty (built-in defaults) config.
  DENY_CFG="$TOOLS/deny-trusted.toml"; DENY_CHECKS="advisories bans sources"
  if ! git show "$(trusted_ref):deny.toml" > "$DENY_CFG" 2>/dev/null || [ ! -s "$DENY_CFG" ]; then
    echo "warning: no trusted deny.toml on $(trusted_ref); running advisories only with default config (bans/sources skipped)"
    : > "$DENY_CFG"; DENY_CHECKS="advisories"
  elif ! cmp -s "$DENY_CFG" deny.toml; then
    echo "note: proposed deny.toml differs from trusted policy; scanning with the trusted policy"
  fi
  deny_findings "$PWD" "$w/h.txt" || { echo "cargo-deny failed on head (fail closed)"; rm -rf "$w"; return 2; }
  if git worktree add --detach "$w/base" "$base" >/dev/null 2>&1 && deny_findings "$w/base" "$w/b.txt"; then :; else echo "base not scannable; empty baseline"; : > "$w/b.txt"; fi
  git worktree remove --force "$w/base" >/dev/null 2>&1 || true
  new="$(comm -13 "$w/b.txt" "$w/h.txt")" || { echo "comm failed (fail closed)"; rm -rf "$w"; return 2; }
  local nh nb; nh=$(wc -l < "$w/h.txt"); nb=$(wc -l < "$w/b.txt")
  rm -rf "$w"
  if [ -n "$new" ]; then echo "NEW cargo-deny errors:"; echo "$new"; return 1; fi
  echo "cargo-deny: no new errors (head $nh, base $nb)"
}

case "${1:-}" in
  gitleaks) run_gitleaks ;;
  deps) run_deps ;;
  *) echo "usage: $0 gitleaks|deps"; exit 64 ;;
esac
