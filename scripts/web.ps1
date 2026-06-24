# hike-finder Web UI launcher (Windows PowerShell / pwsh).
#
# Thin wrapper over the `hike-finder-web` entry point: sets a default Overpass
# contact (only if unset) and starts the local map server. This is long-running
# — press Ctrl+C to stop. Extra args (e.g. --host / --port) are forwarded; the
# server prints its own URL to stdout.
#
#   .\scripts\web.ps1                 # http://127.0.0.1:8765
#   .\scripts\web.ps1 --port 9000
#
# Override the contact by setting it yourself first:
#   $env:HIKE_OVERPASS_UA = "you@example.com"; .\scripts\web.ps1
#
# Requires `pip install -e .` (so `hike-finder-web` is on PATH) in the active venv.
$ErrorActionPreference = 'Stop'
if (-not $env:HIKE_OVERPASS_UA) { $env:HIKE_OVERPASS_UA = 'boikoneov@gmail.com' }
& hike-finder-web @args
exit $LASTEXITCODE
