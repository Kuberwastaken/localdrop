# LocalDrop AirDrop runtime

LocalDrop uses omdrop's actual `/Discover`, `/Ask`, and DVZip `/Upload` sender
and receiver, with an explicit LocalDrop accept/reject prompt before an incoming
transfer. Links are saved as files, never opened automatically. The radio runs
as a separate privileged program; the receiver and bridge run as your user.

AirDrop support currently requires Linux. The same bridge supports either
installed omdrop radio backend at `/usr/lib/omdrop/omdrop-discoverable`:

* **OWL:** omdrop-owl's tested profile is MediaTek MT7925 (`mt7925e`). Wi-Fi must
  be connected on channel 6, 44, or 149. Other monitor/injection-capable cards
  require upstream's explicit untested-hardware opt-in. They are not guaranteed
  to work. Stopping OWL can briefly reconnect the Wi-Fi station.
* **Broadcom:** omdrop-awdl supports Apple's BCM4378, BCM4387 and BCM4388 on
  supported Asahi Linux systems. Its package installs a patched DKMS driver.
  This does not add AWDL to arbitrary PC Broadcom cards.

Windows, macOS, Android and iOS LocalDrop builds still use LocalSend transport.
They do not provide this Linux AWDL bridge. The UI capability probe reports the
platform, missing dependencies and OWL hardware/channel blockers. The older
Broadcom backend checks its actual radio during start.

## Install

Use Python 3.11 or newer. On Arch, install build/runtime dependencies first:

```sh
sudo pacman -S --needed base-devel git cmake libev libpcap libnl python \
  python-libarchive-c python-pillow python-zeroconf python-ifaddr \
  bluez python-dbus python-gobject polkit iproute2 iputils networkmanager keyutils
python3 support/airdrop/install.py --radio owl --install-radio --install
```

In an extracted Linux LocalDrop release, use `python3 airdrop/install.py` in
place of `python3 support/airdrop/install.py`; the same arguments apply.

On a supported Asahi Linux Broadcom system, select `--radio broadcom` instead;
the upstream package additionally requires DKMS and matching Asahi kernel
headers. Install only one radio backend. `--install-radio` builds the chosen
upstream Arch package and installs it through `makepkg -si`; it must be run as a
normal user and will ask for package installation privileges. `--install`
installs the receiver/bridge using sudo. No radio operation runs during staging.

If the appropriate omdrop radio package is already installed, omit
`--install-radio`. On other distributions, install the selected pinned upstream
radio backend and equivalent Python/system dependencies using its instructions,
then run `python3 support/airdrop/install.py --radio owl --install` (or broadcom).
The dependency probe checks the same system Python the launcher uses; a pip
virtual environment alone will not satisfy `/usr/bin/python3` imports.

The installer fetches exact commits, verifies the receiver checksum, and retains
the entire fetched MIT receiver source tree and license. It refuses to reset a
modified checkout. A repeated install needs a fresh `--build-dir`. The radio
package remains an independently installed program at `/usr/lib/omdrop`.

If a firewall is active, permit AirDrop HTTPS TCP 8771 **only on `awdl0`** and the
radio backend's required discovery traffic, following omdrop's firewall docs.
The installer does not weaken an existing firewall. Enable Bluetooth and open
AirDrop on the Apple device; peer naming/send can use the upstream BLE wake.

## Identity and Contacts Only

The receiver and sender retain omdrop's shared `~/.omdrop/keys` identity and
the active identity cache. Existing omdrop setup, disk identities, and optional
1Password-backed identity setup therefore continue to work. LocalDrop does not
obtain Apple credentials or silently generate an Apple-issued identity.
Without an Apple identity, the upstream self-signed identity supports peers
using **Everyone** visibility; Apple **Contacts Only** sending requires a valid
existing Apple identity recognized by the peer.

Incoming Contacts Only policy remains upstream's certificate-bound signed
record verification and known-contact hashes. Configure it through your
installed omdrop CLI (see the fetched `upstream/omdrop-plugin/README.md` and
`docs/identity-contract.md`): visibility and known contacts are read from
`~/.config/omdrop`. Even a verified contact requires the LocalDrop prompt.
Display names are supplied by peers and are not authenticated identities.
This integration does not provide a new Apple account or contact-management UI.

## JSON-lines protocol (version 1)

Execute `/usr/bin/localdrop-airdrop` without a shell. Write one JSON object per
stdin line; stdout contains only JSON objects, and stderr contains diagnostics.
Every command has an `id` echoed in a `response` with `ok`, then either `result`
or `error`. Operations may complete out of order; match IDs.

| Command | Extra fields | Result |
| --- | --- | --- |
| `probe` | none | `available`, `platform`, `reason`, `detail`, optional `backend` |
| `start` | `name`, existing user-owned `downloadDir`, `seconds` (30–600; default 300) | `started`, `seconds` |
| `peers` | none | `peers` snapshot |
| `send` | scanned `peerId`, `paths` (1–1000 regular files) | `started`; transfer finishes asynchronously |
| `decide` | `offerId`, boolean `accept` | `decided` |
| `stop` | none | `stopped` |
| `shutdown` | none | `stopped`; process exits after cleanup |

Events:

* `status`: `state`, `detail`. States include `starting`, `discovering`, `stopped`,
  `error`, `unsupported`, `missing_dependencies`, and `radio_unavailable`.
* `peers`: `peers` array of `{id,name,address,port}`. IDs rotate with AWDL sessions.
  A peer that does not answer is excluded; an unnamed responding peer appears as
  `Apple device`. Reported port comes from the upstream peer scanner.
* `offer`: `offerId`, `sender`, `files` (`{name,size}`), `expiresAt` (Unix milliseconds).
  Unanswered offers expire after 45 seconds and are rejected. At most four are
  pending. No incoming transfer is automatically accepted.
* `transfer`: `direction` (`send` or `receive`), `state` (`sending`, `receiving`,
  `completed`, `failed`, `rejected`, `expired`), optional `peerId`, `offerId`,
  `files`, `detail`. No artificial byte-progress percentages are emitted.

Each upload requires an accepted Ask on the same TLS connection; consent is
consumed once. The pinned receiver retains its archive traversal prevention,
safe names, output-space budgets, bounded request readers, and Contacts Only
checks. Receive content is not run. EOF/SIGTERM/shutdown stop owned processes
and the owned radio session. A separate omdrop session must be stopped before
LocalDrop can start. Discovery automatically ends within ten minutes; a radio
helper also enforces its window if the app crashes. LocalDrop cannot promise
completion of a large transfer beyond the active discoverability window.

## Source and licensing

These external programs are fetched at installation and are not vendored into
the LocalDrop repository or bundled in its default app binaries:

| Program | Pin | License |
| --- | --- | --- |
| omdrop-owl | `4259681af4b552cd67d0b3a36665fc0890586606` | Its repository has no standalone license for its own helper; fetch directly for local installation, do not redistribute that source as LocalDrop-owned code |
| OWL (through omdrop-owl pins) | `832d70f815c3d4a06a02117bf0fc5e868daa1ff0` | GPL-3.0-or-later; radiotap ISC |
| omdrop-awdl portable tools (through omdrop-owl pins) | `534f91a525337951ca19c311e53d333bd6d55100` | GPL-2.0-only |
| omdrop-awdl Broadcom package | `095dd4570bcd108d2763212afda5bd5ae1c2c248` | GPL-2.0-only |
| omdrop-plugin receiver | `80678835713f9837195528a7500cdf70a1b67437` | MIT, retained in full fetched tree |

Radio packaging preserves upstream license files; keep complete corresponding
source and required license notices with any separately redistributed GPL radio
packages. The LocalDrop bridge communicates with them as separate processes.
The consent adapter loads the MIT receiver with tightly checked insertion
points, and refuses any receiver whose SHA-256 differs from its pinned source.

## Validation

```sh
python3 -m unittest discover -s support/airdrop/tests -v
```

Tests cover protocol validation, peer parsing, explicit consent, expired/repeated
decisions, upload consent enforcement and source-adapter contracts. Live AWDL
needs supported Linux radio hardware and an Apple device; offline tests are not
a claim that an on-air send/receive has been verified on every driver.
