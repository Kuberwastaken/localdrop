#!/usr/bin/env python3
"""Keep a child process within its window even if the app process disappears."""
import os
import signal
import subprocess
import sys
import time

from localdrop_bridge import terminate


def main():
    deadline = float(os.environ["LOCALDROP_SESSION_DEADLINE"])
    if len(sys.argv) < 2 or deadline <= time.time():
        return 124
    proc = None
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        proc = subprocess.Popen(sys.argv[1:], start_new_session=True)
        return proc.wait(timeout=max(0.01, deadline - time.time()))
    except subprocess.TimeoutExpired:
        return 124
    except KeyboardInterrupt:
        return 130
    finally:
        terminate(proc)


if __name__ == "__main__":
    raise SystemExit(main())
