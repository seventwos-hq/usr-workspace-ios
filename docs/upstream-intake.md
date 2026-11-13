# Upstream intake (Element X iOS -> Seventwos Workspace iOS)

How to pull a new Element X iOS release into this fork safely. Adapted from the desktop template (usr-workspace-desktop `docs/upstream-intake.md`).

Read `AGENTS.md` and `.seventwos/upstream-policy.json` first; `.seventwos/upstream-ledger.jsonl` is append-only.

## Procedure

1. Fetch upstream tag (unverified; add remote once):
   `git remote add upstream <element-x-ios-upstream-url>` ; `git fetch upstream --tags` (unverified)
2. Branch off develop:
   `git fetch origin && git checkout -b intake/<upstream-tag> origin/develop` (unverified)
3. Merge the tag: `git merge <upstream-tag>` (unverified). Resolve conflicts keeping Seventwos names/ids/config.
4. Re-apply and verify branding:
   - `scripts/brand-guard.sh` must print `brand-guard: OK` (run on current develop: see PR description for result).
   - New legitimate matches only: justify, then `scripts/brand-guard.sh --update` (regenerates baseline in `.brand-guard-allowlist`) and review the diff (unverified). Prefer rebranding over raising a baseline.
   - Check `app.yml` (`APP_DISPLAY_NAME`, `PRODUCTION_APP_NAME`, `BASE_BUNDLE_IDENTIFIER`, `APP_GROUP_IDENTIFIER`) and endpoints in `Seventwos/Sources/Application/Settings/AppSettings.swift`.
5. Quick checks (all unverified; Xcode builds/tests need macOS and cannot run on the Linux dev server):
   - `python3 ci_scripts/validate_upstream_policy.py` (unverified)
   - `python3 ci_scripts/validate_upstream_policy_test.py` (unverified)
   - `ci_scripts/validate_release_readiness.sh` (unverified)
   - `swift run -q tools ci unit-tests` (unverified; macOS/Xcode)
   - `swiftlint`, `swiftformat .` (unverified)
   - Only `documentation/` is OKF-validated (`scripts/okf/validate_okf_markdown.py --changed`, unverified); files in `docs/` need no frontmatter.

6. Push `intake/<upstream-tag>` and open a PR into `develop`. Do not merge without review; do not tag or release.

## Invariants (must hold after every intake)

- App name/ids stay Seventwos (`Seventwos Workspace`, `org.seventwos.workspace`, `group.org.seventwos.workspace`) per `.seventwos/upstream-invariants.json`.
- Default homeserver/endpoints stay `workspace.seventwos.org` in `AppSettings.swift`; no `element.io` / `vector.im` there.
- `scripts/brand-guard.sh` passes.
- No `element.io` / `riot.im` / `app.element.*` URLs outside `.brand-guard-allowlist`.
- Signing team stays empty (`empty-signing-team` invariant).
- Ledger entries are never edited, reordered or removed.
