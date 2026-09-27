# hike-finder MCP server launcher (Windows PowerShell / pwsh), stdio transport.
#
# Thin wrapper over the `hike-finder-mcp` entry point: runs the server. An MCP
# client launches this and speaks JSON-RPC over the process's stdin/stdout.
#
# IMPORTANT: stdout IS the JSON-RPC channel. This script must write NOTHING to
# stdout (no Write-Host/Write-Output, no banner) or the client handshake breaks.
# Diagnostics, if ever needed, go to stderr only.
#
# Point your MCP client at this file, e.g.:
#   claude mcp add hike-finder -- powershell -NoProfile -ExecutionPolicy Bypass `
#     -File M:\claud_projects\hike-finder-mcp\scripts\mcp.ps1
#
# Set your contact in the client's env config (HIKE_OVERPASS_UA) — the OpenStreetMap
# servers ask every program for one, and this repo ships none. This launcher
# can't ask for it: stdin belongs to the client's JSON-RPC stream.
#
# Requires `pip install -e ".[mcp]"` (so `hike-finder-mcp` is on PATH).
$ErrorActionPreference = 'Stop'
& hike-finder-mcp @args
exit $LASTEXITCODE
