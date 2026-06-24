#!/usr/bin/env bash
# hike-finder MCP server launcher (Linux / macOS / Git Bash), stdio transport.
#
# Thin wrapper over the `hike-finder-mcp` entry point: sets a default Overpass
# contact (only if unset), then `exec`s the server so it inherits stdin/stdout
# directly. An MCP client launches this and speaks JSON-RPC over those pipes.
#
# IMPORTANT: stdout IS the JSON-RPC channel. This script must write NOTHING to
# stdout (no echo, no banner) or the client handshake breaks. The `exec` keeps
# the byte stream pristine — no shell sits between client and server.
#
# Point your MCP client at this file, e.g.:
#   claude mcp add hike-finder -- /abs/path/to/hike-finder-mcp/scripts/mcp.sh
#
# Override the contact by exporting HIKE_OVERPASS_UA in the client's env config.
#
# Requires `pip install -e ".[mcp]"` (so `hike-finder-mcp` is on PATH).
set -euo pipefail
: "${HIKE_OVERPASS_UA:=boikoneov@gmail.com}"
export HIKE_OVERPASS_UA
exec hike-finder-mcp "$@"
