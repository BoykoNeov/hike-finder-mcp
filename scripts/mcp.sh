#!/usr/bin/env bash
# hike-finder MCP server launcher (Linux / macOS / Git Bash), stdio transport.
#
# Thin wrapper over the `hike-finder-mcp` entry point: `exec`s the server so it
# inherits stdin/stdout directly. An MCP client launches this and speaks JSON-RPC
# over those pipes.
#
# IMPORTANT: stdout IS the JSON-RPC channel. This script must write NOTHING to
# stdout (no echo, no banner) or the client handshake breaks. The `exec` keeps
# the byte stream pristine — no shell sits between client and server.
#
# Point your MCP client at this file, e.g.:
#   claude mcp add hike-finder -- /abs/path/to/hike-finder-mcp/scripts/mcp.sh
#
# Set your contact in the client's env config (HIKE_OVERPASS_UA) — the OpenStreetMap
# servers ask every program for one, and this repo ships none. This launcher
# can't ask for it: stdin belongs to the client's JSON-RPC stream.
#
# Requires `pip install -e ".[mcp]"` (so `hike-finder-mcp` is on PATH).
set -euo pipefail
exec hike-finder-mcp "$@"
