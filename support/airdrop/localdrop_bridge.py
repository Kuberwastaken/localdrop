#!/usr/bin/env python3
"""LocalDrop JSON-lines AirDrop bridge. Radio and receiver stay separate programs."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

MAX_LINE = 262144
PEER_ROW = re.compile(r"^([0-9a-f]{2}(?::[0-9a-f]{2}){5})\s+.*?\[([^]]+)\]:(\d+)(?:\s+(.*))?$", re.I)


def parse_peers(output):
    found = {}
    for line in output.splitlines():
        match = PEER_ROW.match(line)
        if not match:
            continue
        peer_id, address, port, name = match.groups()
        name = (name or "").strip()
        if name == "(no response)":
            continue
        found[peer_id.lower()] = {"id": peer_id.lower(), "name": name if name and name != "(anonymous)" else "Apple device",
                                   "address": address, "port": int(port)}
    return list(found.values())


def terminate(proc, grace=4):
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=grace)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=4)
        except (OSError, subprocess.TimeoutExpired):
            pass


class Bridge:
    def __init__(self, emit=None, radio_dir="/usr/lib/omdrop", runtime_dir=None):
        self.emit = emit or self.write
        self.radio = Path(radio_dir)
        self.runtime = Path(runtime_dir or Path(__file__).resolve().parent)
        self.output_lock = threading.Lock()
        self.lifecycle = threading.RLock()
        self.offer_lock = threading.Lock()
        self.offers = {}
        self.peer_table = {}
        self.receiver = None
        self.sender = None
        self.listener = None
        self.socket_dir = None
        self.token = None
        self.active = False
        self.generation = 0
        self.owned_radio = False
        self.owned_identity = False
        self.identity_source = None
        self.identity_notice = ""
        self.identity_expiry = None
        self.identity_seq = None
        self.session_expiry = 0
        self.identity_lock_fd = None
        self.deadline = 0
        self.name = "LocalDrop"
        self.closing = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=4)

    def write(self, value):
        with self.output_lock:
            print(json.dumps(value, ensure_ascii=True, separators=(",", ":")), flush=True)

    def status(self, state, detail=""):
        self.emit({"type": "status", "state": state, "detail": detail})

    def run(self, argv, timeout=25):
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=os.name == "posix")
        try:
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            terminate(proc)
            raise RuntimeError("AirDrop operation timed out") from None

    def helper(self, *args, privileged=False, timeout=25):
        argv = [str(self.radio / "omdrop-discoverable"), *args]
        if privileged:
            argv.insert(0, "pkexec")
        return self.run(argv, timeout)

    def identity(self, *args, timeout=10):
        command = self.runtime / "upstream/omdrop-plugin/bin/identity.py"
        rc, out, err = self.run([sys.executable, str(command), *args], timeout=timeout)
        if rc != 0:
            raise RuntimeError((err or out).strip()[-2000:] or "AirDrop identity operation failed")
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def identity_paths(self):
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        state = runtime / "omdrop"
        return state / "identity.lock", state / "window"

    def acquire_identity_lock(self):
        import fcntl
        lock_path, _ = self.identity_paths()
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + 30
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Another omdrop identity operation is still running")
                    time.sleep(0.1)
        except Exception:
            os.close(fd)
            raise

    def release_identity_lock(self):
        if self.identity_lock_fd is not None:
            os.close(self.identity_lock_fd)
            self.identity_lock_fd = None

    def prepare_identity(self, name):
        # begin handles legacy migration and uses op only when previously
        # configured for 1Password; it never obtains an Apple identity itself.
        prepared = self.identity("begin", "--name", name, timeout=75)
        seq = prepared.get("seq", "")
        if not seq.isdigit():
            raise RuntimeError("Invalid identity preparation response")
        self.identity_lock_fd = self.acquire_identity_lock()
        try:
            _, window_path = self.identity_paths()
            if window_path.exists():
                raise RuntimeError("Another omdrop identity window is open; stop it before enabling LocalDrop")
            armed = self.identity("window", "--seq", seq)
            self.owned_identity = True
            self.identity_seq = int(seq)
            source = armed.get("source")
            if source not in ("disk", "self-signed", "1password"):
                raise RuntimeError("Invalid identity window response")
            self.identity_source = source
            self.identity_notice = "; ".join(x for x in (prepared.get("notice"), armed.get("notice")) if x)
            self.identity_expiry = None
            if source == "1password":
                remaining = armed.get("expiry_in", "")
                if not remaining.isdigit() or int(remaining) < 5:
                    raise RuntimeError("The cached Apple identity expires too soon; unlock it again before enabling AirDrop")
                # expiry_in is rounded against upstream's integer clock. A
                # one-second margin avoids extending the hard expiry by the
                # caller's fractional second or CLI return latency.
                self.identity_expiry = time.time() + int(remaining) - 1
        except Exception:
            try:
                if self.owned_identity:
                    self.clear_identity()
            finally:
                self.release_identity_lock()
            raise

    def clear_identity(self):
        if not self.owned_identity:
            return
        acquired = self.identity_lock_fd is None
        if acquired:
            self.identity_lock_fd = self.acquire_identity_lock()
        try:
            if self.identity_is_current():
                try:
                    self.identity("stop-begin")
                finally:
                    # stop-begin bumps the shared sequence. Keep the new owner
                    # token even when stop-end fails, so a retry can finish our
                    # cleanup without confusing it with a replacement window.
                    _, window = self.identity_paths()
                    try:
                        state = dict(line.split("=", 1) for line in (window.parent / "state").read_text().splitlines() if "=" in line)
                    except FileNotFoundError:
                        state = {}
                    if state.get("seq") == str(self.identity_seq + 1):
                        self.identity_seq += 1
                self.identity("stop-end")
            self.owned_identity = False
            self.identity_source = None
            self.identity_expiry = None
            self.identity_seq = None
        finally:
            if acquired:
                self.release_identity_lock()

    def identity_is_current(self):
        _, window = self.identity_paths()
        try:
            state = dict(line.split("=", 1) for line in (window.parent / "state").read_text().splitlines() if "=" in line)
        except FileNotFoundError:
            state = {}
        return window.exists() and state.get("seq", "0") == str(self.identity_seq)

    def probe(self):
        result = {"available": False, "platform": platform.system().lower(), "reason": "unsupported", "detail": "AirDrop radio support requires Linux and supported Wi-Fi hardware."}
        if platform.system() != "Linux":
            return result
        if sys.version_info < (3, 11):
            return {**result, "reason": "missing_dependencies", "detail": "AirDrop requires Python 3.11 or newer in the system interpreter used by /usr/bin/localdrop-airdrop."}
        missing = [str(self.radio / f) for f in ("omdrop-discoverable", "send-to-peer", "airdrop-send.py") if not (self.radio / f).is_file()]
        receiver_source = self.runtime / "upstream/omdrop-plugin/bin/airdrop-serve.py"
        if not receiver_source.is_file():
            missing.append(str(receiver_source))
        missing.extend(x for x in ("pkexec", "ip", "openssl") if not shutil.which(x))
        missing.extend(x for x in ("libarchive", "PIL", "zeroconf", "ifaddr") if importlib.util.find_spec(x) is None)
        if missing:
            return {**result, "reason": "missing_dependencies", "detail": "Install the AirDrop runtime: " + ", ".join(missing)}
        rc, out, err = self.helper("probe", "--json")
        if rc == 0:
            try:
                radio = json.loads(out)
                blocked = radio.get("missing", [])
                if blocked or not radio.get("hardware", True):
                    return {**result, "reason": "radio_unavailable", "detail": "; ".join(x.get("say", "Radio unavailable") for x in blocked), "backend": radio.get("backend")}
                return {**result, "available": True, "reason": "ready", "detail": "AirDrop radio is ready.", "backend": radio.get("backend")}
            except (ValueError, AttributeError, TypeError):
                raise RuntimeError("Invalid radio capability response") from None
        # omdrop-awdl 0.8.1 predates probe. Its start command performs the real
        # hardware checks; never describe this fallback as proven radio health.
        rc, out, err = self.helper("status", "--json")
        if rc == 0:
            try:
                status = json.loads(out)
            except ValueError:
                raise RuntimeError("Invalid radio status response") from None
            if "visible" in status:
                return {**result, "available": True, "reason": "ready", "backend": "omdrop-awdl", "detail": "Installed Broadcom backend; hardware will be checked when enabling AirDrop."}
        return {**result, "reason": "radio_unavailable", "detail": (err or out).strip()[-2000:]}

    def start(self, command):
        with self.lifecycle:
            if self.closing.is_set():
                raise RuntimeError("Bridge is shutting down")
            if self.active:
                raise ValueError("AirDrop is already enabled")
            capability = self.probe()
            if not capability["available"]:
                self.status(capability["reason"], capability["detail"])
                raise RuntimeError(capability["detail"])
            name = command.get("name", "LocalDrop")
            if not isinstance(name, str) or not name.strip() or len(name) > 63 or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in name):
                raise ValueError("name must be 1–63 characters without control characters or separators")
            seconds = command.get("seconds", 300)
            if type(seconds) is not int or not 30 <= seconds <= 600:
                raise ValueError("seconds must be an integer from 30 to 600")
            download_dir = Path(command.get("downloadDir", "")).expanduser().resolve()
            if not command.get("downloadDir") or not download_dir.is_dir() or download_dir.stat().st_uid != os.getuid():
                raise ValueError("downloadDir must be an existing directory owned by the current user")
            # Do not take ownership of another omdrop session or extend it.
            rc, out, _ = self.helper("status", "--json")
            if rc != 0 or json.loads(out).get("visible"):
                raise RuntimeError("Another AirDrop radio session is running; stop it before enabling LocalDrop")
            self.status("starting", "Enabling the AirDrop radio")
            self.name = name
            self.prepare_identity(name)
            try:
                if self.closing.is_set():
                    raise RuntimeError("Bridge is shutting down")
                # Hold the upstream identity lock until the receiver and radio
                # are running, matching omdrop's window/start contract.
                rc, out, _ = self.helper("status", "--json")
                if rc != 0 or json.loads(out).get("visible"):
                    raise RuntimeError("Another AirDrop radio session started while preparing the identity")
                actual_seconds = seconds
                if self.identity_expiry is not None:
                    actual_seconds = min(seconds, max(1, int(self.identity_expiry - time.time())))
                rc, out, err = self.helper("start", str(actual_seconds), privileged=True, timeout=90)
                if rc != 0:
                    if rc != 5:
                        # A failed start can leave a partial interface behind.
                        self.helper("stop", privileged=True, timeout=45)
                    raise RuntimeError((err or out).strip()[-2000:] or "Radio start failed")
                self.owned_radio = True
                self.session_expiry = time.time() + actual_seconds
                if self.identity_expiry is not None:
                    self.session_expiry = min(self.session_expiry, self.identity_expiry)
                if self.session_expiry <= time.time() + 1:
                    raise RuntimeError("The Apple identity expired while starting the radio")
                self.socket_dir = tempfile.TemporaryDirectory(prefix="localdrop-airdrop-")
                os.chmod(self.socket_dir.name, 0o700)
                socket_path = str(Path(self.socket_dir.name) / "consent.sock")
                self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.listener.bind(socket_path)
                os.chmod(socket_path, 0o600)
                self.listener.listen(8)
                self.listener.settimeout(1)
                self.token = secrets.token_hex(32)
                env = {**os.environ, "LOCALDROP_CONSENT_SOCKET": socket_path, "LOCALDROP_CONSENT_TOKEN": self.token,
                       "LOCALDROP_RECEIVER": str(self.runtime / "upstream/omdrop-plugin/bin/airdrop-serve.py"),
                       "LOCALDROP_SESSION_DEADLINE": str(self.session_expiry)}
                self.active = True
                self.generation += 1
                session = self.generation
                self.deadline = time.monotonic() + max(0, self.session_expiry - time.time())
                threading.Thread(target=self.accept_loop, daemon=True).start()
                # Receiver diagnostics go to stderr; stdout remains JSON only.
                ready = threading.Event()
                self.receiver = subprocess.Popen([sys.executable, str(self.runtime / "localdrop_lifetime.py"),
                                                  sys.executable, str(self.runtime / "localdrop_receiver.py"), "--name", name,
                                                  "--outdir", str(download_dir), "--config", "/dev/null"],
                                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, start_new_session=True)
                threading.Thread(target=self.receiver_log, args=(self.receiver, ready), daemon=True).start()
                startup_deadline = time.monotonic() + min(35, seconds - 5)
                while not ready.wait(0.1) and self.receiver.poll() is None and time.monotonic() < startup_deadline:
                    if self.closing.is_set():
                        raise RuntimeError("Bridge is shutting down")
                if not ready.is_set() or self.receiver.poll() is not None:
                    raise RuntimeError("AirDrop receiver failed to start; see its diagnostic log")
                self.status("discovering", self.discovery_detail("AirDrop enabled for " + str(actual_seconds) + " seconds"))
                threading.Thread(target=self.monitor, args=(session,), daemon=True).start()
                return {"started": True, "seconds": actual_seconds, "identitySource": self.identity_source, "identityNotice": self.identity_notice}
            except Exception:
                self.stop()
                raise
            finally:
                self.release_identity_lock()

    def discovery_detail(self, detail):
        if self.identity_notice:
            return detail + ". " + self.identity_notice
        return detail

    @staticmethod
    def receiver_log(proc, ready):
        for line in proc.stdout:
            sys.stderr.write(line)
            sys.stderr.flush()
            if "serving " in line and "._airdrop._tcp.local" in line:
                ready.set()

    def accept_loop(self):
        listener = self.listener
        while self.active and not self.closing.is_set():
            try:
                conn, _ = listener.accept()
                threading.Thread(target=self.consent_connection, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def consent_connection(self, conn):
        with conn:
            conn.settimeout(50)
            try:
                with conn.makefile("rb") as stream:
                    line = stream.readline(MAX_LINE + 1)
                if len(line) > MAX_LINE:
                    return
                message = json.loads(line)
                if not secrets.compare_digest(str(message.get("token", "")), self.token or "") or not self.active:
                    return
                if message.get("kind") == "event":
                    self.emit({"type": "transfer", "direction": "receive", **{k: message[k] for k in ("offerId", "state", "files", "detail") if k in message}})
                    conn.sendall(b'{"ok":true}\n')
                    return
                if message.get("kind") != "offer":
                    return
                offer_id = secrets.token_hex(16)
                pending = {"event": threading.Event(), "accept": False, "deadline": time.monotonic() + 45}
                with self.offer_lock:
                    if len(self.offers) >= 4:
                        conn.sendall(b'{"accept":false}\n')
                        return
                    self.offers[offer_id] = pending
                ask = message["ask"]
                self.emit({"type": "offer", "offerId": offer_id, "sender": ask["sender"], "files": ask["files"], "expiresAt": int((time.time() + 45) * 1000)})
                decided = pending["event"].wait(45)
                with self.offer_lock:
                    self.offers.pop(offer_id, None)
                accepted = decided and pending["accept"] and self.active
                if not accepted:
                    self.emit({"type": "transfer", "direction": "receive", "offerId": offer_id, "state": "rejected" if decided else "expired"})
                conn.sendall(json.dumps({"accept": accepted, "offerId": offer_id}).encode() + b"\n")
            except (OSError, ValueError, KeyError, TypeError):
                return

    def decide(self, command):
        if type(command.get("accept")) is not bool:
            raise ValueError("accept must be a boolean")
        with self.offer_lock:
            pending = self.offers.get(command.get("offerId"))
            if not pending or time.monotonic() >= pending["deadline"] or pending["event"].is_set():
                raise ValueError("Offer expired or was already decided")
            pending["accept"] = command["accept"]
            pending["event"].set()
        return {"decided": True}

    def current_session(self, session):
        return self.active and self.generation == session

    def peers(self, session=None):
        with self.lifecycle:
            if session is None:
                session = self.generation
            if not self.current_session(session):
                return {"peers": []}
            name = self.name
        rc, out, err = self.run([sys.executable, str(self.radio / "send-to-peer"), "--list", "--names", "--name", name], timeout=45)
        # A scan may finish after stop/start. Its result and failure both belong
        # to the old session; neither may alter the new session's UI or peers.
        with self.lifecycle:
            if not self.current_session(session):
                return {"peers": []}
            if rc not in (0, 1):
                raise RuntimeError((err or out).strip()[-2000:] or "Peer scan failed")
            peers = parse_peers(out)
            self.peer_table = {p["id"]: p for p in peers}
            self.emit({"type": "peers", "peers": peers})
            return {"peers": peers}

    def send(self, command):
        with self.lifecycle:
            if self.closing.is_set():
                raise RuntimeError("Bridge is shutting down")
            if not self.active or (self.sender is not None and self.sender.poll() is None):
                raise ValueError("Enable AirDrop and wait for any current send to finish")
            peer_id = command.get("peerId")
            if peer_id not in self.peer_table:
                raise ValueError("Select a peer from the current scan")
            paths = command.get("paths")
            if not isinstance(paths, list) or not 1 <= len(paths) <= 1000 or not all(isinstance(p, str) for p in paths):
                raise ValueError("paths must contain 1–1000 file paths")
            files = [Path(p).expanduser().resolve() for p in paths]
            if not all(p.is_file() for p in files):
                raise ValueError("Every send path must be an existing regular file")
            argv = [sys.executable, str(self.radio / "send-to-peer"), "--mac", peer_id, "--name", self.name,
                    "--port", str(self.peer_table[peer_id]["port"]), "--op-timeout", "60", "--wait", "20", "--", *map(str, files)]
            argv = [sys.executable, str(self.runtime / "localdrop_lifetime.py"), *argv]
            env = {**os.environ, "LOCALDROP_SESSION_DEADLINE": str(self.session_expiry)}
            self.sender = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True, env=env)
            proc = self.sender
            self.emit({"type": "transfer", "direction": "send", "peerId": peer_id, "state": "sending"})
            threading.Thread(target=self.finish_send, args=(proc, peer_id), daemon=True).start()
            return {"started": True}

    def finish_send(self, proc, peer_id):
        try:
            output, _ = proc.communicate(timeout=600)
            detail = output[-3000:] if proc.returncode else "Transfer sent"
        except subprocess.TimeoutExpired:
            terminate(proc, grace=12)
            detail = "Transfer timed out"
        self.emit({"type": "transfer", "direction": "send", "peerId": peer_id,
                   "state": "completed" if proc.returncode == 0 else "failed", "detail": detail})

    def monitor(self, session):
        while self.current_session(session) and not self.closing.wait(8):
            with self.lifecycle:
                if not self.current_session(session):
                    return
                if time.monotonic() >= self.deadline:
                    self.stop()
                    return
                if self.owned_identity and not self.identity_is_current():
                    self.stop()
                    return
                if self.receiver is None or self.receiver.poll() is not None:
                    self.status("error", "AirDrop receiver stopped unexpectedly")
                    self.stop()
                    return
            try:
                self.peers(session)
            except Exception as exc:
                with self.lifecycle:
                    if self.current_session(session):
                        # Discovery remains enabled after a recoverable scan
                        # failure, so the UI must retain its Stop action.
                        self.status("discovering", self.discovery_detail("Peer scan failed; retrying: " + str(exc)))

    def stop(self):
        with self.lifecycle:
            self.active = False
            self.generation += 1
            # Local processes and consent do not require the shared lock.
            # Always stop them, even when another app holds identity.lock.
            self.stop_local_processes()
            acquired = self.owned_identity and self.identity_lock_fd is None
            if acquired:
                try:
                    self.identity_lock_fd = self.acquire_identity_lock()
                except Exception as exc:
                    detail = "Local transfers stopped; retry Stop to finish radio and identity cleanup: " + str(exc)
                    self.status("discovering", detail)
                    raise RuntimeError(detail) from None
            try:
                return self.stop_runtime()
            finally:
                if acquired:
                    self.release_identity_lock()

    def stop_runtime(self):
        with self.lifecycle:
            if self.owned_identity and not self.identity_is_current():
                # omdrop off/identity lock may have replaced our window with a
                # different session. Tear down our processes without stopping
                # that other session's radio or clearing its identity.
                self.owned_radio = False
            if self.owned_radio:
                rc, out, err = self.helper("stop", privileged=True, timeout=45)
                if rc != 0:
                    # Retain the ownership window for a safe locked retry.
                    detail = "Local transfers stopped; retry Stop to finish radio cleanup: " + ((err or out).strip()[-2000:] or "Radio cleanup failed")
                    self.status("discovering", detail)
                    raise RuntimeError(detail)
                self.owned_radio = False
            try:
                self.clear_identity()
            except Exception as exc:
                detail = "Local transfers stopped; retry Stop to finish identity cleanup: " + str(exc)
                self.status("discovering", detail)
                raise RuntimeError(detail) from None
            self.status("stopped")
            return {"stopped": True}

    def stop_local_processes(self):
        with self.offer_lock:
            pending_offers = list(self.offers.items())
            self.offers.clear()
            for _, pending in pending_offers:
                pending["accept"] = False
                pending["event"].set()
        for offer_id, _ in pending_offers:
            self.emit({"type": "transfer", "direction": "receive", "offerId": offer_id, "state": "rejected"})
        # A lifetime guard spends up to eight seconds terminating its child
        # group. Let it finish before forcibly killing the outer guard.
        terminate(self.sender, grace=12)
        terminate(self.receiver, grace=12)
        self.sender = self.receiver = None
        if self.listener:
            self.listener.close()
            self.listener = None
        if self.socket_dir:
            self.socket_dir.cleanup()
            self.socket_dir = None
        self.peer_table = {}
        self.emit({"type": "peers", "peers": []})

    def handle(self, command):
        request_id = command.get("id")
        try:
            action = command.get("command")
            if action == "probe":
                result = self.probe()
            elif action == "start":
                result = self.start(command)
            elif action == "stop":
                result = self.stop()
            elif action == "peers":
                result = self.peers()
            elif action == "send":
                result = self.send(command)
            elif action == "decide":
                result = self.decide(command)
            else:
                raise ValueError("Unknown command")
            self.emit({"type": "response", "id": request_id, "ok": True, "result": result})
        except Exception as exc:
            self.emit({"type": "response", "id": request_id, "ok": False, "error": str(exc)})

    def close(self):
        self.closing.set()
        try:
            self.stop()
        except Exception as exc:
            self.status("error", str(exc))
        self.pool.shutdown(wait=True, cancel_futures=True)


def main():
    bridge = Bridge()
    # EOF, explicit shutdown, SIGINT and SIGTERM all end the finite window.
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    if os.name == "posix":
        signal.signal(signal.SIGTERM, interrupted)
    try:
        while not bridge.closing.is_set():
            line = sys.stdin.buffer.readline(MAX_LINE + 1)
            if not line:
                break
            try:
                if len(line) > MAX_LINE or not line.endswith(b"\n"):
                    raise ValueError("Input must be one bounded JSON line")
                command = json.loads(line)
                if not isinstance(command, dict):
                    raise ValueError("Command must be an object")
                if command.get("command") == "shutdown":
                    bridge.emit({"type": "response", "id": command.get("id"), "ok": True, "result": {"stopped": True}})
                    break
                if command.get("command") == "decide":
                    bridge.handle(command)
                else:
                    bridge.pool.submit(bridge.handle, command)
            except ValueError as exc:
                bridge.emit({"type": "response", "id": None, "ok": False, "error": str(exc)})
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
