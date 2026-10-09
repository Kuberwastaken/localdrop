# Contributing to LocalDrop

LocalDrop is an independently maintained LocalSend fork. Report LocalDrop bugs
and submit pull requests at https://github.com/Kuberwastaken/localdrop.
AI-assisted work is allowed here; contributors remain responsible for correctness,
source attribution, tests, and accurate compatibility claims.

Use the pinned Flutter and Rust versions. Keep LocalSend protocol compatibility
and internal package names intact unless a change requires otherwise. Follow
AGENTS.md for the app, isolate and Rust architecture. Build and test instructions
are in README.md and support/airdrop/README.md.

AirDrop changes need tests for consent, session ownership and cleanup. Include
hardware, driver, kernel and Apple OS versions when reporting on-air results.
Do not treat offline protocol tests as proof of radio compatibility. Retain
upstream licenses; source fetched by the AirDrop installer stays separate from
the app source.

For a release, keep app/pubspec.yaml, cli/Cargo.toml, Cargo.lock and packaging
versions in sync. Run CI and the release workflow against the intended commit.
Create the matching version tag and publish artifacts only after validation.
Android signing uses GitHub repository secrets LOCALDROP_ANDROID_KEYSTORE and
LOCALDROP_ANDROID_KEY_PASSWORD; never commit keys or credentials. Desktop bundles
are unsigned community builds (macOS uses a local ad-hoc signature).

Changes intended for upstream LocalSend must follow its own contribution policy.
The original upstream guide is retained in docs/LocalSend-CONTRIBUTING.md.
