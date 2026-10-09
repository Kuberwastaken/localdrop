import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


bridge_mod = module("localdrop_bridge")
receiver_mod = module("localdrop_receiver")
lifetime_mod = module("localdrop_lifetime")


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

    def test_python_310_probe_reports_prerequisite_before_radio_checks(self):
        with patch.object(bridge_mod.platform, "system", return_value="Linux"), patch.object(bridge_mod.sys, "version_info", (3, 10, 12)), patch.object(self.bridge, "helper") as helper:
            result = self.bridge.probe()
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "missing_dependencies")
        self.assertIn("Python 3.11", result["detail"])
        helper.assert_not_called()

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
        terminate.assert_any_call(receiver, grace=12)
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

    def test_configured_1password_identity_arms_and_clears_upstream_window(self):
        with tempfile.TemporaryDirectory() as temp:
            window = Path(temp) / "window"
            def identity(*args, **kwargs):
                if args[0] == "begin":
                    return {"seq": "7"}
                if args[0] == "window":
                    window.write_text("source=1password\n")
                    (window.parent / "state").write_text("seq=7\n")
                    return {"source": "1password", "expiry_in": "120"}
                if args[0] == "stop-begin":
                    (window.parent / "state").write_text("seq=8\n")
                if args[0] == "stop-end":
                    window.unlink()
                return {}
            with patch.object(self.bridge, "identity", side_effect=identity) as identity_call, patch.object(self.bridge, "identity_paths", return_value=(Path(temp) / "identity.lock", window)), patch.object(self.bridge, "acquire_identity_lock", return_value=123), patch.object(self.bridge, "release_identity_lock", side_effect=lambda: setattr(self.bridge, "identity_lock_fd", None)):
                before = time.time()
                self.bridge.prepare_identity("LocalDrop")
                self.assertEqual(self.bridge.identity_source, "1password")
                self.assertTrue(self.bridge.owned_identity)
                self.assertGreaterEqual(self.bridge.identity_expiry, before + 119)
                self.assertLess(self.bridge.identity_expiry, time.time() + 120)
                self.bridge.clear_identity()
                self.assertFalse(self.bridge.owned_identity)
                self.assertEqual([call.args for call in identity_call.call_args_list], [("begin", "--name", "LocalDrop"), ("window", "--seq", "7"), ("stop-begin",), ("stop-end",)])

    def test_existing_upstream_identity_window_is_not_adopted_or_cleared(self):
        with tempfile.TemporaryDirectory() as temp:
            window = Path(temp) / "window"
            window.write_text("source=disk\n")
            with patch.object(self.bridge, "identity", return_value={"seq": "7"}) as identity_call, patch.object(self.bridge, "identity_paths", return_value=(Path(temp) / "identity.lock", window)), patch.object(self.bridge, "acquire_identity_lock", return_value=123), patch.object(self.bridge, "release_identity_lock") as release, self.assertRaises(RuntimeError):
                self.bridge.prepare_identity("LocalDrop")
            self.assertEqual(len(identity_call.call_args_list), 1)
            self.assertFalse(self.bridge.owned_identity)
            self.assertTrue(window.exists())
            release.assert_called_once()

    def test_identity_fallback_notice_persists_in_discovery_details(self):
        with tempfile.TemporaryDirectory() as temp:
            window = Path(temp) / "window"
            notice = '1Password locked; using self-signed certificates. Peers must be in Everyone mode.'
            with patch.object(self.bridge, "identity", side_effect=[{"seq": "1", "notice": notice}, {"source": "self-signed"}]), patch.object(self.bridge, "identity_paths", return_value=(Path(temp) / "identity.lock", window)), patch.object(self.bridge, "acquire_identity_lock", return_value=123):
                self.bridge.prepare_identity("LocalDrop")
            self.assertEqual(self.bridge.identity_source, "self-signed")
            self.assertIn(notice, self.bridge.discovery_detail("AirDrop enabled"))
            self.assertIn(notice, self.bridge.discovery_detail("Peer scan failed; retrying"))

    def test_stop_does_not_clear_replacement_identity_or_radio_session(self):
        self.bridge.owned_identity = True
        self.bridge.owned_radio = True
        self.bridge.identity_lock_fd = 123
        with patch.object(self.bridge, "identity_is_current", return_value=False), patch.object(self.bridge, "identity") as identity, patch.object(self.bridge, "helper") as helper:
            self.bridge.stop()
        identity.assert_not_called()
        helper.assert_not_called()
        self.assertFalse(self.bridge.owned_identity)
        self.assertFalse(self.bridge.owned_radio)

    def test_lock_timeout_stops_local_processes_and_rejects_offers(self):
        self.bridge.active = True
        self.bridge.owned_identity = True
        self.bridge.owned_radio = True
        sender, receiver = Mock(), Mock()
        self.bridge.sender, self.bridge.receiver = sender, receiver
        pending = {"event": threading.Event(), "accept": True, "deadline": time.monotonic() + 45}
        self.bridge.offers["pending"] = pending
        with patch.object(self.bridge, "acquire_identity_lock", side_effect=RuntimeError("lock busy")), patch.object(bridge_mod, "terminate") as terminate, patch.object(self.bridge, "helper") as helper, patch.object(self.bridge, "identity") as identity, self.assertRaisesRegex(RuntimeError, "retry Stop"):
            self.bridge.stop()
        terminate.assert_any_call(sender, grace=12)
        terminate.assert_any_call(receiver, grace=12)
        self.assertFalse(pending["accept"])
        self.assertTrue(pending["event"].is_set())
        self.assertEqual(self.bridge.offers, {})
        self.assertIsNone(self.bridge.receiver)
        self.assertIsNone(self.bridge.sender)
        self.assertTrue(self.bridge.owned_radio)
        self.assertTrue(self.bridge.owned_identity)
        helper.assert_not_called()
        identity.assert_not_called()
        self.assertEqual(self.events[-1]["state"], "discovering")

    def test_radio_stop_failure_preserves_identity_window_for_retry(self):
        self.bridge.owned_identity = True
        self.bridge.owned_radio = True
        self.bridge.identity_lock_fd = 123
        with patch.object(self.bridge, "identity_is_current", return_value=True), patch.object(self.bridge, "helper", return_value=(1, "", "stop failed")), patch.object(self.bridge, "clear_identity") as clear_identity, self.assertRaisesRegex(RuntimeError, "retry Stop"):
            self.bridge.stop()
        clear_identity.assert_not_called()
        self.assertTrue(self.bridge.owned_identity)
        self.assertTrue(self.bridge.owned_radio)

    def test_stop_end_failure_retains_bumped_sequence_for_retry(self):
        self.bridge.owned_identity = True
        self.bridge.identity_seq = 7
        self.bridge.identity_lock_fd = 123
        with tempfile.TemporaryDirectory() as temp:
            window = Path(temp) / "window"
            window.write_text("source=disk\n")
            state = window.parent / "state"
            state.write_text("seq=7\n")
            failed = [False]
            def identity(*args, **kwargs):
                if args[0] == "stop-begin":
                    state.write_text("seq=" + str(self.bridge.identity_seq + 1) + "\n")
                if args[0] == "stop-end":
                    if not failed[0]:
                        failed[0] = True
                        raise RuntimeError("stop-end timed out")
                    window.unlink()
                return {}
            with patch.object(self.bridge, "identity", side_effect=identity), patch.object(self.bridge, "identity_paths", return_value=(Path(temp) / "identity.lock", window)):
                with self.assertRaises(RuntimeError):
                    self.bridge.clear_identity()
                self.assertEqual(self.bridge.identity_seq, 8)
                self.assertTrue(self.bridge.owned_identity)
                self.bridge.clear_identity()
            self.assertFalse(window.exists())
            self.assertFalse(self.bridge.owned_identity)


class LifetimeTests(unittest.TestCase):
    def test_hard_deadline_terminates_child_even_without_bridge(self):
        proc = Mock()
        proc.wait.side_effect = subprocess.TimeoutExpired("receiver", 10)
        with patch.dict(os.environ, {"LOCALDROP_SESSION_DEADLINE": "110"}), patch.object(lifetime_mod.time, "time", return_value=100), patch.object(lifetime_mod.sys, "argv", ["localdrop_lifetime.py", "python3", "receiver.py"]), patch.object(lifetime_mod.subprocess, "Popen", return_value=proc) as popen, patch.object(lifetime_mod.signal, "signal"), patch.object(lifetime_mod, "terminate") as terminate:
            self.assertEqual(lifetime_mod.main(), 124)
        popen.assert_called_once_with(["python3", "receiver.py"], start_new_session=True)
        proc.wait.assert_called_once_with(timeout=10)
        terminate.assert_called_once_with(proc)

    def test_expired_window_does_not_start_child(self):
        with patch.dict(os.environ, {"LOCALDROP_SESSION_DEADLINE": "90"}), patch.object(lifetime_mod.time, "time", return_value=100), patch.object(lifetime_mod.sys, "argv", ["localdrop_lifetime.py", "python3", "receiver.py"]), patch.object(lifetime_mod.subprocess, "Popen") as popen:
            self.assertEqual(lifetime_mod.main(), 124)
        popen.assert_not_called()


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
