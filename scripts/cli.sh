#!/usr/bin/env bash
# hike-finder CLI launcher (Linux / macOS / Git Bash).
#
# Thin wrapper over the `hike-finder` entry point: it sets a default Overpass
# contact (only if you haven't already) and forwards every argument unchanged.
# Results print to stdout, so this adds NO banner of its own.
#
#   ./scripts/cli.sh --bbox 50.72 15.58 50.74 15.62
#   ./scripts/cli.sh --bbox 50.72 15.58 50.74 15.62 --circular --json
#
# Override the contact by exporting it first:
#   HIKE_OVERPASS_UA=you@example.com ./scripts/cli.sh ...
#
# Requires `pip install -e .` (so `hike-finder` is on PATH) in the active venv.
set -euo pipefail
: "${HIKE_OVERPASS_UA:=boikoneov@gmail.com}"
export HIKE_OVERPASS_UA
exec hike-finder "$@"
