import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


bridge_mod = module("localdrop_bridge")
receiver_mod = module("localdrop_receiver")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.bridge = bridge_mod.Bridge(emit=self.events.append)

    def tearDown(self):
        self.bridge.active = False
        self.bridge.pool.shutdown(wait=True, cancel_futures=True)

    def test_unsupported_probe_does_not_execute_program(self):
        with patch.object(bridge_mod.platform, "system", return_value="Windows"), patch.object(self.bridge, "run") as run:
            result = self.bridge.probe()
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "unsupported")
        run.assert_not_called()

    def test_peer_parser_ignores_logs_unreachable_and_deduplicates(self):
        parsed = bridge_mod.parse_peers("""some debugging text
12:34:56:78:90:ab  ? dBm  [fe80::1%awdl0]:8770  Jane's iPhone
12:34:56:78:90:ab  ? dBm  [fe80::1%awdl0]:8770  Jane's iPhone
aa:bb:cc:dd:ee:ff  -30 dBm  [fe80::2%awdl0]:8770  (anonymous)
aa:bb:cc:dd:ee:aa  ? dBm  [fe80::3%awdl0]:8770  (no response)
""")
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["name"], "Jane's iPhone")
        self.assertEqual(parsed[1]["name"], "Apple device")

    def test_decisions_require_boolean_and_are_single_use(self):
        pending = {"event": threading.Event(), "accept": False, "deadline": time.monotonic() + 45}
        self.bridge.offers["one"] = pending
        with self.assertRaises(ValueError):
            self.bridge.decide({"offerId": "one", "accept": "true"})
        self.bridge.decide({"offerId": "one", "accept": True})
        self.assertTrue(pending["accept"])
        with self.assertRaises(ValueError):
            self.bridge.decide({"offerId": "one", "accept": False})

    def test_expired_decision_rejects(self):
        self.bridge.offers["old"] = {"event": threading.Event(), "accept": False, "deadline": time.monotonic() - 1}
        with self.assertRaises(ValueError):
            self.bridge.decide({"offerId": "old", "accept": True})

    def test_socket_consent_blocks_until_explicit_decision(self):
        self.bridge.active = True
        self.bridge.token = "secret"
        server, client = socket.socketpair()
        worker = threading.Thread(target=self.bridge.consent_connection, args=(server,))
        worker.start()
        with client:
            client.settimeout(2)
            client.sendall(json.dumps({"token": "secret", "kind": "offer", "ask": {"sender": "Apple device", "files": [{"name": "photo.jpg", "size": 100}]}}).encode() + b"\n")
            until = time.monotonic() + 2
            while not self.events and time.monotonic() < until:
                time.sleep(0.01)
            self.assertEqual(self.events[0]["type"], "offer")
            offer_id = self.events[0]["offerId"]
            self.assertFalse(self.bridge.offers[offer_id]["event"].is_set())
            self.bridge.decide({"offerId": offer_id, "accept": False})
            result = json.loads(client.recv(4096))
            self.assertFalse(result["accept"])
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.events[-1]["state"], "rejected")

    def test_invalid_socket_token_never_offers(self):
        self.bridge.active = True
        self.bridge.token = "secret"
        server, client = socket.socketpair()
        worker = threading.Thread(target=self.bridge.consent_connection, args=(server,))
        worker.start()
        with client:
            client.sendall(b'{"token":"wrong","kind":"offer"}\n')
        worker.join(2)
        self.assertEqual(self.events, [])

    def test_unanswered_socket_offer_expires_without_accepting(self):
        self.bridge.active = True
        self.bridge.token = "secret"
        conn = Mock()
        conn.__enter__ = Mock(return_value=conn)
        conn.__exit__ = Mock(return_value=False)
        conn.makefile.return_value = io.BytesIO(b'{"token":"secret","kind":"offer","ask":{"sender":"Phone","files":[{"name":"photo","size":1}]}}\n')
        expired = Mock()
        expired.wait.return_value = False
        with patch.object(bridge_mod.threading, "Event", return_value=expired):
            self.bridge.consent_connection(conn)
        response = json.loads(conn.sendall.call_args.args[0])
        self.assertFalse(response["accept"])
        self.assertEqual(self.events[-1]["state"], "expired")
        self.assertEqual(self.bridge.offers, {})

    def test_pending_offer_limit_rejects_without_prompt(self):
        self.bridge.active = True
        self.bridge.token = "secret"
        self.bridge.offers = {str(i): {} for i in range(4)}
        conn = Mock()
        conn.__enter__ = Mock(return_value=conn)
        conn.__exit__ = Mock(return_value=False)
        conn.makefile.return_value = io.BytesIO(b'{"token":"secret","kind":"offer","ask":{"sender":"Phone","files":[]}}\n')
        self.bridge.consent_connection(conn)
        self.assertFalse(json.loads(conn.sendall.call_args.args[0])["accept"])
        self.assertEqual(self.events, [])

    def test_unknown_command_returns_correlated_error(self):
        self.bridge.handle({"id": 12, "command": "execute", "shell": "bad"})
        self.assertEqual(self.events[0]["id"], 12)
        self.assertFalse(self.events[0]["ok"])

    def test_send_cannot_use_unscanned_peer(self):
        self.bridge.active = True
        with patch.object(bridge_mod.subprocess, "Popen") as popen, self.assertRaises(ValueError):
            self.bridge.send({"peerId": "malicious;command", "paths": ["/tmp/file"]})
        popen.assert_not_called()

    def test_send_uses_argv_and_option_separator_for_file(self):
        self.bridge.active = True
        self.bridge.peer_table["12:34:56:78:90:ab"] = {"port": 8770}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "-dangerous.txt"
            path.write_text("hello")
            proc = Mock()
            proc.poll.return_value = 0
            with patch.object(bridge_mod.subprocess, "Popen", return_value=proc) as popen, patch.object(bridge_mod.threading, "Thread"):
                self.bridge.send({"peerId": "12:34:56:78:90:ab", "paths": [str(path)]})
            argv = popen.call_args.args[0]
            self.assertEqual(argv[-2], "--")
            self.assertEqual(argv[-1], str(path.resolve()))
            self.assertNotIn("shell", popen.call_args.kwargs)

    def test_stop_rejects_pending_offer_and_only_stops_owned_radio(self):
        pending = {"event": threading.Event(), "accept": True, "deadline": time.monotonic() + 45}
        self.bridge.offers["one"] = pending
        with patch.object(self.bridge, "helper") as helper:
            self.bridge.stop()
        self.assertFalse(pending["accept"])
        self.assertTrue(pending["event"].is_set())
        helper.assert_not_called()

    def test_shutdown_prevents_late_start(self):
        self.bridge.closing.set()
        with patch.object(self.bridge, "probe") as probe, self.assertRaises(RuntimeError):
            self.bridge.start({})
        probe.assert_not_called()

    def test_recoverable_scan_failure_keeps_discovery_and_stop_available(self):
        self.bridge.active = True
        self.bridge.deadline = time.monotonic() + 300
        self.bridge.receiver = Mock()
        self.bridge.receiver.poll.return_value = None
        with patch.object(self.bridge.closing, "wait", side_effect=[False, True]), patch.object(self.bridge, "peers", side_effect=RuntimeError("scan timeout")):
            self.bridge.monitor(self.bridge.generation)
        self.assertTrue(self.bridge.active)
        self.assertEqual(self.events, [{"type": "status", "state": "discovering", "detail": "Peer scan failed; retrying: scan timeout"}])
        receiver = self.bridge.receiver
        with patch.object(bridge_mod, "terminate") as terminate:
            self.bridge.stop()
        terminate.assert_any_call(receiver)
        self.assertFalse(self.bridge.active)
        self.assertEqual(self.events[-1]["state"], "stopped")

    def test_old_monitor_cannot_stop_restarted_session(self):
        self.bridge.active = True
        old_session = self.bridge.generation
        self.bridge.deadline = time.monotonic() - 1
        def restart_during_wait(seconds):
            self.bridge.generation += 1
            # Deliberately leave a deadline that would make the old monitor
            # stop the replacement session without a generation check.
            return False
        with patch.object(self.bridge.closing, "wait", side_effect=restart_during_wait), patch.object(self.bridge, "stop") as stop, patch.object(self.bridge, "peers") as peers:
            self.bridge.monitor(old_session)
        stop.assert_not_called()
        peers.assert_not_called()
        self.assertEqual(self.events, [])
        self.assertTrue(self.bridge.active)

    def test_scan_finishing_after_restart_cannot_publish_stale_peers(self):
        self.bridge.active = True
        replacement = {"new-peer": {"id": "new-peer"}}
        def scan(argv, timeout):
            self.bridge.stop()
            self.bridge.active = True
            self.bridge.generation += 1
            self.bridge.peer_table = replacement.copy()
            self.events.clear()
            return 0, "12:34:56:78:90:ab  ? dBm  [fe80::1%awdl0]:8770  Old phone\n", ""
        with patch.object(self.bridge, "run", side_effect=scan):
            result = self.bridge.peers()
        self.assertEqual(result, {"peers": []})
        self.assertEqual(self.bridge.peer_table, replacement)
        self.assertEqual(self.events, [])

    def test_failure_from_stale_scan_is_ignored(self):
        self.bridge.active = True
        def scan(argv, timeout):
            self.bridge.generation += 1
            return 4, "", "Old session failure"
        with patch.object(self.bridge, "run", side_effect=scan):
            result = self.bridge.peers()
        self.assertEqual(result, {"peers": []})
        self.assertEqual(self.events, [])


SOURCE = '''class Handler(Base):
    def reject(self, code, reason):
        self.rejection = (code, reason)
        self.send_response(code)
    def handle_ask(self, body):
        self.ask_called = True
        self.send_response(200)
    def handle_upload(self):
        self.upload_called = True
        if True:
            if True:
                written = store_upload(raw, DEST, tid, receive_budget)
        self.send_response(200)
'''


class ReceiverConsentTests(unittest.TestCase):
    def make_handler(self, accepted=True):
        self.events = []
        class Base:
            def send_response(self, code, message=None):
                pass
        namespace = {"Base": Base, "plistlib": plistlib, "MAX_ARCHIVE_MEMBERS": 1000,
                     "_localdrop_approve": lambda ask: {"accept": accepted, "offerId": "offer"},
                     "_localdrop_event": lambda **kw: self.events.append(kw),
                     "store_upload": lambda *args: ["photo.jpg"], "raw": None, "DEST": None, "tid": None, "receive_budget": None}
        exec(compile(receiver_mod.adapt(SOURCE), "fixture", "exec"), namespace)
        return namespace["Handler"]()

    def ask(self, handler):
        handler.handle_ask(plistlib.dumps({"SenderComputerName": "Phone", "Files": [{"FileName": "photo.jpg", "FileSize": 10}]}))

    def test_upload_without_consent_never_reaches_storage(self):
        handler = self.make_handler()
        handler.handle_upload()
        self.assertEqual(handler.rejection[0], 403)
        self.assertFalse(getattr(handler, "upload_called", False))

    def test_declined_offer_never_calls_upstream(self):
        handler = self.make_handler(accepted=False)
        self.ask(handler)
        self.assertEqual(handler.rejection[0], 403)
        self.assertFalse(getattr(handler, "ask_called", False))

    def test_accepted_upload_consumes_consent_once_and_reports_files(self):
        handler = self.make_handler()
        self.ask(handler)
        handler.handle_upload()
        self.assertTrue(handler.upload_called)
        self.assertEqual(self.events[-1], {"offerId": "offer", "state": "completed", "files": ["photo.jpg"]})
        handler.upload_called = False
        handler.handle_upload()
        self.assertEqual(handler.rejection[0], 403)
        self.assertFalse(handler.upload_called)

    def test_invalid_ask_is_rejected_before_prompt(self):
        handler = self.make_handler()
        handler.handle_ask(plistlib.dumps({"Files": [{"FileName": "photo", "FileSize": -1}]}))
        self.assertEqual(handler.rejection[0], 400)

    def test_adapter_refuses_missing_contract_anchor(self):
        with self.assertRaises(ValueError):
            receiver_mod.adapt(SOURCE.replace("def handle_ask", "def other"))

    def test_receiver_hash_mismatch_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "receiver.py"
            source.write_text("raise AssertionError('must never execute')")
            with patch.dict(os.environ, {"LOCALDROP_RECEIVER": str(source)}), self.assertRaises(SystemExit):
                receiver_mod.main()


if __name__ == "__main__":
    unittest.main()
