# LocalDrop privacy

LocalDrop transfers files and messages directly between devices. The LocalSend
transport uses your local network; its discovery broadcasts your device alias,
device type and protocol information to nearby devices. Encrypted transfers use
per-device certificates. Turning encryption off in Settings removes that protection.

Linux AirDrop uses separately installed omdrop radio and protocol helpers. During
an AirDrop window, nearby devices can discover your selected alias and device model.
Peer lookup can reveal your presence to nearby Apple devices. Incoming transfers
require approval in LocalDrop; an unanswered request is rejected. Received files
are saved in the folder shown in the AirDrop tab.

AirDrop identity material and any existing omdrop contact records remain local to
the omdrop runtime. LocalDrop does not ask for an Apple ID or upload contact data
to a LocalDrop service. If you separately configure an omdrop Apple identity, its
own identity and contact handling applies.

Settings, device certificates, saved devices and transfer history are stored on
your device. The app contains no LocalDrop analytics collector. Links opened from
the app, dependency installation and release downloads contact their respective
external services. Diagnostic information you choose to share may include device
and network details; review it before posting publicly.

The inherited WebRTC feature is disabled in this release. LocalDrop does not
operate a public signaling service.

Source and issue reporting: https://github.com/Kuberwastaken/localdrop
