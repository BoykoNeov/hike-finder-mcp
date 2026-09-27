#!/usr/bin/env bash
# hike-finder CLI launcher (Linux / macOS / Git Bash).
#
# Thin wrapper over the `hike-finder` entry point: it forwards every argument
# unchanged. Results print to stdout, so this adds NO banner of its own.
#
#   ./scripts/cli.sh --bbox 50.72 15.58 50.74 15.62
#   ./scripts/cli.sh --bbox 50.72 15.58 50.74 15.62 --circular --json
#
# Set your own contact first — the OpenStreetMap servers ask every program for
# one, and this repo ships none (without it a generic identifier is sent):
#   export HIKE_OVERPASS_UA=you@example.com
#
# Requires `pip install -e .` (so `hike-finder` is on PATH) in the active venv.
set -euo pipefail
exec hike-finder "$@"
