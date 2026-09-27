#!/usr/bin/env bash
# hike-finder Web UI launcher (Linux / macOS / Git Bash).
#
# Thin wrapper over the `hike-finder-web` entry point: starts the local map
# server. This is long-running — press Ctrl+C to stop. Extra args (e.g. --host /
# --port / --open) are forwarded; the server prints its own URL to stdout.
#
#   ./scripts/web.sh                # http://127.0.0.1:8765
#   ./scripts/web.sh --port 9000
#
# Set your own contact first — the OpenStreetMap servers ask every program for
# one, and this repo ships none (without it a generic identifier is sent):
#   export HIKE_OVERPASS_UA=you@example.com
# (or type it into the page's Contact box).
#
# Requires `pip install -e .` (so `hike-finder-web` is on PATH) in the active venv.
set -euo pipefail
exec hike-finder-web "$@"
