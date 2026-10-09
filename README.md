# LocalDrop

<img src="docs/localdrop-icon.png" alt="LocalDrop icon" width="112" height="112">

Local file sharing, with experimental Apple AirDrop compatibility on Linux.
LocalDrop is an independent fork of [LocalSend](https://github.com/localsend/localsend).
It keeps the LocalSend protocol and adds an AirDrop tab backed by
[omdrop-owl](https://github.com/t4t5/omdrop-owl) and the omdrop ecosystem.

[Download releases](https://github.com/Kuberwastaken/localdrop/releases) ·
[AirDrop setup](support/airdrop/README.md) · [Privacy](PRIVACY.md) · [License](LICENSE)

## What works where

| Platform | LocalSend transfers | Apple AirDrop transport |
| --- | --- | --- |
| Windows | Yes | Not available |
| Linux | Yes | Supported omdrop radio hardware; separate runtime installation |
| macOS | Yes | Not available in LocalDrop; macOS has its own native AirDrop |
| Android | Yes | Not available |
| iOS (source build) | Yes | Not available |

LocalSend devices and LocalDrop devices can exchange files and messages over
the same local network without a cloud account. Apple devices running LocalSend
or LocalDrop can use this route too.

Linux AirDrop transfers use AWDL rather than the LocalSend protocol. LocalDrop
supports both omdrop radio backends: OWL on monitor/injection-capable adapters,
and the patched Broadcom backend on supported Asahi Linux systems. This does
not make every Wi-Fi adapter AirDrop-capable. OWL's tested profile is the
MediaTek MT7925; it needs a connected access point on channel 6, 44 or 149.
Other OWL adapters require upstream's explicit untested-hardware opt-in.
The capability check reports prerequisites before a session starts.

The AirDrop tab provides nearby-device discovery, file sending, received-file
folder selection, and explicit Accept/Reject prompts. Discovery runs for five
minutes and can be stopped early. Incoming requests expire and are rejected
if unanswered. Received files and links are saved; they are never launched
automatically. Existing omdrop identities and contact policies are retained.
See [the runtime documentation](support/airdrop/README.md) for Contacts Only
requirements, firewall rules, supported radios, and the complete JSON protocol.

## Install and use

Download the package for your platform from Releases. Windows and macOS bundles
are community builds without a commercial code signature or Apple notarization.
Android APKs use a persistent LocalDrop signing key and a separate application
ID, so they can coexist with LocalSend. The Linux archive contains the app and
the AirDrop runtime installer; the privileged radio package is installed separately.

For normal transfers, open LocalDrop on both devices, keep them on the same
network, select files in Send, and choose the receiving device. Allow TCP and UDP
port 53317 on your local network if a firewall blocks discovery or transfers.

For Linux AirDrop, follow [AirDrop setup](support/airdrop/README.md). Start
LocalDrop, open AirDrop, choose a receive folder and enable the five-minute
window. Turn on Wi-Fi and Bluetooth on the Apple device and choose Everyone
for 10 Minutes unless you have configured a compatible omdrop identity.
Incoming requests require your approval in LocalDrop.

## Build from source

Use the Flutter version pinned in `.fvmrc` (3.41.9) and the Rust toolchain in
`rust-toolchain.toml` (1.97.1), with platform-specific Flutter build prerequisites.

```sh
git clone https://github.com/Kuberwastaken/localdrop.git
cd localdrop/app
flutter pub get
flutter run
```

Release builds remove the inherited proprietary purchase dependency using
`sh support/scripts/remove_proprietary_dependencies.sh` before resolving pub
dependencies. Standard build commands are `flutter build windows`,
`flutter build linux`, `flutter build macos`, and `flutter build apk` in `app/`.
Android release builds require your own `app/android/key.properties` and signing
key. Never commit signing keys or passwords.

The command-line LocalSend client is built with
`cargo build --release -p localsend-cli` at the repository root and produces
`localdrop-cli`. Sending and the default receive mode use LocalSend transport.

## Checks and releases

```sh
cd app
flutter analyze
flutter test
cd ../packages/localsend_isolates
flutter test
cd ../core
cargo test --features full
cd ../..
python3 -m unittest discover -s support/airdrop/tests -v
```

GitHub Actions checks Dart formatting, static analysis, Flutter and Rust tests,
AirDrop bridge tests, and packaging/version consistency. The release workflow
builds platform artifacts from a matching version tag; release publication is
a separate maintainer step after checks and builds succeed. Linux AWDL needs a
supported radio and an Apple device for on-air validation. Automated protocol
and consent tests do not establish compatibility with every Apple OS or driver.

## Credits and licensing

LocalSend is by Tien Do Nam and its contributors. LocalDrop preserves its
Apache-2.0 license and original Git history. The fork starts at
`c1ce322fb3acf08e44f329b8b7208b8b0d91e244`. The new LocalDrop icon has an
[editable SVG master](docs/localdrop-icon.svg) and a
[platform icon generator](support/branding/generate_icons.py).

The Linux AirDrop integration invokes separately installed upstream programs:
omdrop-plugin (MIT), omdrop-awdl (GPL-2.0-only), OWL (GPL-3.0-or-later), and
radiotap (ISC). The installer fetches pinned sources and preserves attribution.
omdrop-owl's own helper has no standalone repository license, so its source is
fetched directly for local installation rather than redistributed here.
See [NOTICE](NOTICE) and [runtime licensing](support/airdrop/README.md#source-and-licensing).

LocalDrop is not affiliated with Apple or the LocalSend project. AirDrop is an
Apple trademark. The [original LocalSend README](docs/LocalSend-README.md) is
retained as upstream reference.
