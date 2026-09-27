# hike-finder CLI launcher (Windows PowerShell / pwsh).
#
# Thin wrapper over the `hike-finder` entry point: it forwards every argument
# unchanged. Results print to stdout, so this adds NO banner of its own.
#
#   .\scripts\cli.ps1 --bbox 50.72 15.58 50.74 15.62
#   .\scripts\cli.ps1 --bbox 50.72 15.58 50.74 15.62 --circular --json
#
# Set your own contact first — the OpenStreetMap servers ask every program for
# one, and this repo ships none (without it a generic identifier is sent):
#   $env:HIKE_OVERPASS_UA = "you@example.com"; .\scripts\cli.ps1 ...
#
# Requires `pip install -e .` (so `hike-finder` is on PATH) in the active venv.
$ErrorActionPreference = 'Stop'
& hike-finder @args
exit $LASTEXITCODE
