# Element-controlled build-time dependencies

Inventory of Swift packages and binaries pulled at build time from Element, element-hq, or matrix-org sources. Static review of `project.yml`, `Package.swift` files, the three `Package.resolved` files, and `ci_scripts/`. Nothing was built or fetched.

Sources: app pins are in `project.yml` (`packages:`) and resolved in `Seventwos.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved`. There is no Gemfile, fastlane, Podfile or Cartfile.

## App dependencies (`project.yml`)

| Package | URL | Pin (resolved revision) | What it is | Upstream source | Recommendation |
|---|---|---|---|---|---|
| MatrixRustSDK | github.com/element-hq/matrix-rust-components-swift | exact 26.09.09 (resolved 26.9.9, `56ec8d0458a6`) | SwiftPM wrapper around a **prebuilt binary xcframework** of the Matrix Rust SDK (Swift bindings) | github.com/matrix-org/matrix-rust-sdk (Rust, built via UniFFI; wrapper repo has build scripts) | Keep with revision pin now; the binary target checksum lives in the wrapper's Package.swift at that revision. Medium term: build from pinned matrix-rust-sdk source and use a local `path:` package (a commented `path:` hook already exists). Cannot be replaced by another SDK. |
| EmbeddedElementCall | github.com/element-hq/element-call-swift | exact 0.25.0 (`48ba4408d60d`) | Packaged web bundle of the Element Call widget (JS/HTML) embedded in the app for calls | github.com/element-hq/element-call (web app, built with yarn/vite) | Keep with revision pin. Build from pinned source is possible but needs a Node toolchain and a repackaging step. Calls feature cannot be replaced without a different RTC client. |
| WysiwygComposer | github.com/element-hq/matrix-rich-text-editor-swift | exact 2.42.0 (`c0877fb9323d`) | Rich-text composer; wrapper with prebuilt Rust xcframework | github.com/matrix-org/matrix-rich-text-editor (Rust + Swift) | Keep with revision pin. Build from source possible (path hook in `project.yml`). Replaceable only by dropping rich-text composing or writing a new composer. |
| Compound (local) -> compound-design-tokens | github.com/element-hq/compound-design-tokens | exact 11.0.0 (`86c15ef71798`), via `compound-ios/Package.swift` | Design tokens (colours, spacing, fonts) consumed by the in-repo `compound-ios` | github.com/element-hq/compound-design-tokens (generated Swift) | Keep with revision pin. Source-only (no binary), so low risk. Replace with Seventwos-owned tokens when rebranding the design system. |
| SwiftOGG | github.com/element-hq/swift-ogg | exact 0.0.4 (`a00a22b90f62`) | Ogg/Opus encode/decode for voice messages; may bundle prebuilt libopus/libogg binaries | github.com/element-hq/swift-ogg | Keep with revision pin; verify whether it ships a binary target and record its checksum. Build from pinned source if a binary is present. Replaceable with another Opus codec wrapper at moderate cost. |
| AnalyticsEvents | github.com/matrix-org/matrix-analytics-events | exact 0.37.0 (`4b143b14a16a`) | Generated Swift event types for analytics | github.com/matrix-org/matrix-analytics-events | Keep with revision pin. Source-only. Analytics is disabled in `Secrets.swift`, so this could be removed if analytics is dropped. Replaceable. |
| Emojibase | github.com/matrix-org/emojibase-bindings | revision `60bc01f2e3b3` (pinned; upstream issue #59) | Swift bindings and data for emoji | github.com/matrix-org/emojibase-bindings (data from emojibase.dev) | Keep revision pin (already a SHA). Replaceable with a vendored emoji dataset. |

## Tooling dependency (`Package.swift`, root, dev tools only)

| Package | URL | Pin | What it is | Recommendation |
|---|---|---|---|---|
| swift-command-line-tools | github.com/element-hq/swift-command-line-tools.git | `branch: main` (resolved `e1e7939200f4`) | Helper library for the `tools` CLI | Floating branch: change to `revision:` (or fork into the openclaw org). `Package.resolved` currently holds the lock, but any re-resolve moves it. Replaceable (small helper code). |

## In-repo code of Element origin (not fetched)

- `compound-ios/` is vendored in this repo (its only Element remote dependency is compound-design-tokens above).
- `renovate-xcodegen.yml` ignores compound-ios, compound-design-tokens, matrix-rich-text-editor-swift and element-call-swift for license listing; these are not auto-listed in the settings bundle and need manual license attribution.

## Binary and script downloads

- `ci_scripts/ci_common.sh` downloads SwiftFormat 0.63.1 from github.com/nicklockwood (third party, not Element) with a SHA-256 check. No Element binaries are downloaded by scripts.
- Binary payloads arrive only through SwiftPM: MatrixRustSDK and WysiwygComposer (xcframeworks), EmbeddedElementCall (web bundle), possibly SwiftOGG.

## Findings

1. All Element-sourced app packages use exact versions, and the resolved file records revisions. Pins are reproducible as long as upstream tags are not moved. SwiftPM does not verify revision on its own against `project.yml`; consider using `revision:` for the binary-bearing packages.
2. Only `swift-command-line-tools` floats (`branch: main`).
3. Binary xcframework checksums are inside upstream wrapper Package.swift files and were not reviewed here (no network fetch). Follow-up: record each binary target URL and checksum.
4. Hosting risk: every source is on GitHub under element-hq or matrix-org. Mirroring pinned revisions into Forgejo would remove the external availability dependency.

## Cannot replace (without losing the feature)

- MatrixRustSDK (core Matrix client; build from source is possible, replacement is not)
- EmbeddedElementCall (calls)
- WysiwygComposer (rich-text composer, unless the feature is dropped)

## Counts

- Element/matrix-org app packages: 7 (5 element-hq direct including compound-design-tokens, 2 matrix-org)
- Plus 1 tooling package (swift-command-line-tools)
- Packages shipping prebuilt binaries/bundles: 3 confirmed, 1 possible (SwiftOGG)
