#!/bin/sh
set -eu

# Preserve the build interface without GNU/BSD sed differences.
SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$SCRIPT_DIR/remove_proprietary_dependencies.py" "$@"
fi
exec python "$SCRIPT_DIR/remove_proprietary_dependencies.py" "$@"
