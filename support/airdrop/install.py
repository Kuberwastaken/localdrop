#!/usr/bin/env python3
"""Fetch pinned, separate AirDrop programs; optionally install on Arch Linux."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys

PINS = {
    "omdrop-owl": ("https://github.com/t4t5/omdrop-owl.git", "4259681af4b552cd67d0b3a36665fc0890586606"),
    "omdrop-awdl": ("https://github.com/brentkearney/omdrop-awdl.git", "095dd4570bcd108d2763212afda5bd5ae1c2c248"),
    "omdrop-plugin": ("https://github.com/brentkearney/omdrop-plugin.git", "80678835713f9837195528a7500cdf70a1b67437"),
}
RECEIVER_SHA256 = "b58377255aa5073377ca2bcf60c89413d3c108cfa8584a57598f6acdd8924bbd"


def run(argv, cwd=None):
    subprocess.run(list(map(str, argv)), cwd=cwd, check=True)


def checkout(root, name):
    url, revision = PINS[name]
    target = root / name
    if target.exists():
        # Refuse to reset or erase an existing working checkout.
        actual = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(target), "status", "--porcelain"], text=True).strip()
        if actual != revision or dirty:
            raise SystemExit(f"{target} exists with different or modified content; choose a fresh --build-dir")
        return target
    run(["git", "init", target])
    run(["git", "-C", target, "remote", "add", "origin", url])
    run(["git", "-C", target, "-c", "core.autocrlf=false", "fetch", "--depth", "1", "origin", revision])
    run(["git", "-C", target, "-c", "core.autocrlf=false", "checkout", "--detach", "FETCH_HEAD"])
    actual = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
    if actual != revision:
        raise SystemExit("Fetched revision does not match pin")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--radio", choices=("owl", "broadcom"), default="owl", help="Select hardware backend; only one can be installed")
    parser.add_argument("--install-radio", action="store_true", help="Build/install selected upstream Arch package via makepkg -si (Broadcom installs a DKMS module)")
    parser.add_argument("--install", action="store_true", help="Install bridge and fetched receiver with sudo under /usr/lib/localdrop/airdrop")
    parser.add_argument("--build-dir", type=Path, default=Path("work/localdrop-airdrop-install"))
    args = parser.parse_args()
    if platform.system() != "Linux":
        raise SystemExit("The AirDrop runtime installer requires Linux")
    if not shutil.which("git"):
        raise SystemExit("Install git first")
    root = args.build_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    upstream = root / "upstream"
    upstream.mkdir(exist_ok=True)
    radio_source = checkout(upstream, "omdrop-owl" if args.radio == "owl" else "omdrop-awdl")
    receiver_source = checkout(upstream, "omdrop-plugin")
    receiver = receiver_source / "bin/airdrop-serve.py"
    if hashlib.sha256(receiver.read_bytes()).hexdigest() != RECEIVER_SHA256:
        raise SystemExit("Pinned receiver checksum mismatch")
    if args.install_radio:
        if not shutil.which("makepkg"):
            raise SystemExit("Automatic radio packaging requires Arch makepkg; see README for other distributions")
        run(["makepkg", "-si", "--noconfirm"], cwd=radio_source)
    stage = root / "runtime"
    if stage.exists():
        raise SystemExit(f"{stage} already exists; choose a fresh --build-dir")
    (stage / "upstream").mkdir(parents=True)
    # Keep the complete MIT source tree and license, rather than selecting
    # files and losing the certificate/layout dependencies or attribution.
    shutil.copytree(receiver_source, stage / "upstream/omdrop-plugin", ignore=shutil.ignore_patterns(".git", "__pycache__"))
    own_source = Path(__file__).resolve().parent
    for name in ("localdrop_bridge.py", "localdrop_receiver.py", "README.md"):
        shutil.copy2(own_source / name, stage / name)
    (stage / "pins.json").write_text(json.dumps(PINS, indent=2) + "\n")
    launcher = root / "localdrop-airdrop"
    launcher.write_text('#!/usr/bin/python3\nimport runpy\nrunpy.run_path("/usr/lib/localdrop/airdrop/localdrop_bridge.py", run_name="__main__")\n')
    launcher.chmod(0o755)
    if args.install:
        run(["sudo", "install", "-d", "-m", "755", "/usr/lib/localdrop/airdrop"])
        run(["sudo", "cp", "-a", str(stage) + "/.", "/usr/lib/localdrop/airdrop/"])
        run(["sudo", "chown", "-R", "root:root", "/usr/lib/localdrop/airdrop"])
        run(["sudo", "chmod", "-R", "go-w", "/usr/lib/localdrop/airdrop"])
        run(["sudo", "install", "-m", "755", launcher, "/usr/bin/localdrop-airdrop"])
    print(f"Prepared runtime: {stage}")
    print(f"Selected radio source: {radio_source}")
    print("The radio helper must be installed at /usr/lib/omdrop/omdrop-discoverable.")


if __name__ == "__main__":
    main()
