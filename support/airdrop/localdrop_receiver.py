#!/usr/bin/env python3
"""Run the separately installed omdrop receiver with a mandatory consent gate.

The pinned upstream source is loaded in its own process. No upstream source is
distributed in LocalDrop. The adapter refuses an unexpected receiver revision.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys

RECEIVER_SHA256 = "b58377255aa5073377ca2bcf60c89413d3c108cfa8584a57598f6acdd8924bbd"
MAX_MESSAGE = 262144


def exchange(message):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(50)
        conn.connect(os.environ["LOCALDROP_CONSENT_SOCKET"])
        message["token"] = os.environ["LOCALDROP_CONSENT_TOKEN"]
        conn.sendall(json.dumps(message).encode() + b"\n")
        with conn.makefile("rb") as stream:
            raw = stream.readline(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE:
            raise ValueError("consent response exceeds limit")
        return json.loads(raw)


def approve(ask):
    try:
        return exchange({"kind": "offer", "ask": ask})
    except (OSError, ValueError, KeyError, TypeError):
        return {"accept": False}


def event(**fields):
    try:
        exchange({"kind": "event", **fields})
    except (OSError, ValueError, KeyError, TypeError):
        pass


ASK_GATE = '''    def handle_ask(self, body):
        self._localdrop_offer = None
        try:
            ask = plistlib.loads(body)
            if not isinstance(ask, dict):
                raise ValueError('Ask must be a dictionary')
            # Only safe display fields cross the consent socket: plist binary
            # identity records remain in the receiver process.
            files = ask.get('Files') or []
            items = ask.get('Items') or []
            if not isinstance(files, list) or not isinstance(items, list):
                raise ValueError('invalid files or items')
            if len(files) + len(items) > MAX_ARCHIVE_MEMBERS:
                raise ValueError('too many offered items')
            rows = []
            for f in files:
                if not isinstance(f, dict):
                    raise ValueError('invalid file')
                size = f.get('FileSize', 0)
                if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                    raise ValueError('invalid size')
                rows.append({'name': str(f.get('FileName', 'file'))[:1024], 'size': size})
            rows.extend({'name': str(i)[:1024], 'size': 0} for i in items)
            if not rows:
                raise ValueError('empty offer')
            proposal = {'sender': str(ask.get('SenderComputerName') or 'Apple device')[:256],
                        'files': rows}
        except Exception:
            self.reject(400, 'invalid Ask')
            return
        answer = _localdrop_approve(proposal)
        if not answer.get('accept'):
            self.reject(403, 'LocalDrop user declined or consent expired')
            return
        self._localdrop_offer = answer['offerId']
        self._localdrop_status = 0
        self._upstream_handle_ask(body)
        if self._localdrop_status != 200:
            _localdrop_event(offerId=self._localdrop_offer, state='failed', detail='Upstream policy refused the offer')
            self._localdrop_offer = None
        elif 'links' in (ask.get('TransferType') or {}):
            _localdrop_event(offerId=self._localdrop_offer, state='completed', files=rows)
            self._localdrop_offer = None

    def handle_upload(self):
        offer = getattr(self, '_localdrop_offer', None)
        if not offer:
            self.reject(403, 'Upload requires a user-accepted Ask on this connection')
            return
        # Consume once before reading: a repeated Upload cannot reuse consent.
        self._localdrop_offer = None
        self._localdrop_status = 0
        self._localdrop_written = []
        _localdrop_event(offerId=offer, state='receiving')
        try:
            self._upstream_handle_upload()
        finally:
            _localdrop_event(offerId=offer,
                            state='completed' if self._localdrop_status == 200 else 'failed',
                            files=self._localdrop_written)

    def send_response(self, code, message=None):
        self._localdrop_status = code
        super().send_response(code, message)

    def _upstream_handle_ask(self, body):'''


def adapt(source):
    replacements = {
        "    def handle_ask(self, body):": ASK_GATE,
        "    def handle_upload(self):": "    def _upstream_handle_upload(self):",
        "                written = store_upload(raw, DEST, tid, receive_budget)":
        "                written = store_upload(raw, DEST, tid, receive_budget)\n                self._localdrop_written = written",
    }
    # Replace Upload first; the new consent wrapper also contains that name.
    for old in reversed(replacements):
        if source.count(old) != 1:
            raise ValueError("upstream receiver contract changed")
        source = source.replace(old, replacements[old], 1)
    return source


def main():
    source_path = Path(os.environ.get("LOCALDROP_RECEIVER", "/usr/lib/localdrop/airdrop/upstream/omdrop-plugin/bin/airdrop-serve.py"))
    raw = source_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != RECEIVER_SHA256:
        raise SystemExit("LocalDrop receiver revision mismatch; reinstall the pinned runtime")
    namespace = {"__name__": "__main__", "__file__": str(source_path),
                 "_localdrop_approve": approve, "_localdrop_event": event}
    exec(compile(adapt(raw.decode()), str(source_path), "exec"), namespace)


if __name__ == "__main__":
    main()
