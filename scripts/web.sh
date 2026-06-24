#!/usr/bin/env bash
# hike-finder Web UI launcher (Linux / macOS / Git Bash).
#
# Thin wrapper over the `hike-finder-web` entry point: sets a default Overpass
# contact (only if unset) and starts the local map server. This is long-running
# — press Ctrl+C to stop. Extra args (e.g. --host / --port) are forwarded; the
# server prints its own URL to stdout.
#
#   ./scripts/web.sh                # http://127.0.0.1:8765
#   ./scripts/web.sh --port 9000
#
# Override the contact by exporting it first:
#   HIKE_OVERPASS_UA=you@example.com ./scripts/web.sh
#
# Requires `pip install -e .` (so `hike-finder-web` is on PATH) in the active venv.
set -euo pipefail
: "${HIKE_OVERPASS_UA:=boikoneov@gmail.com}"
export HIKE_OVERPASS_UA
exec hike-finder-web "$@"
