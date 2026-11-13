# Signing configuration (Seventwos)

No team ID, certificate, profile or key is committed. The Apple Developer team
and certificates are held by Alvin; supply them locally or in release automation.

## Variables (defined in `app.yml`, consumed by all targets)

| Variable | Default | Purpose |
|---|---|---|
| `DEVELOPMENT_TEAM` | `""` (empty; simulator builds need no signing) | Apple team ID. Must stay empty in git (see `.seventwos/upstream-invariants.json`, `empty-signing-team`). |
| `BASE_BUNDLE_IDENTIFIER` | `org.seventwos.workspace` | App bundle id. Extensions derive from it (`.nse`, `.shareextension`, `.maplibre-*`, test targets). |
| `APP_GROUP_IDENTIFIER` | `group.org.seventwos.workspace` | App group entitlement. |
| `KEYCHAIN_ACCESS_GROUP_IDENTIFIER` | `$(DEVELOPMENT_TEAM).$(BASE_BUNDLE_IDENTIFIER)` | Derived automatically from the two above. |

## Overriding without editing tracked files

Pass overrides on the xcodebuild command line (they take precedence over the
generated project settings), with values from environment or CI secrets:

```
xcodebuild ... \
  DEVELOPMENT_TEAM="$SEVENTWOS_TEAM_ID" \
  BASE_BUNDLE_IDENTIFIER="$SEVENTWOS_BUNDLE_ID" \
  APP_GROUP_IDENTIFIER="$SEVENTWOS_APP_GROUP" \
  CODE_SIGN_IDENTITY="$SEVENTWOS_SIGN_IDENTITY"
```

`SEVENTWOS_*` are suggested env var names for your automation, not read by the repo.
Certificates/profiles belong in the keychain or CI secret store, never in git.
Changing the bundle id or app group requires matching App IDs/profiles in the Apple team.
