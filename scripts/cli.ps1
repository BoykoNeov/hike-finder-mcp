# hike-finder CLI launcher (Windows PowerShell / pwsh).
#
# Thin wrapper over the `hike-finder` entry point: it sets a default Overpass
# contact (only if you haven't already) and forwards every argument unchanged.
# Results print to stdout, so this adds NO banner of its own.
#
#   .\scripts\cli.ps1 --bbox 50.72 15.58 50.74 15.62
#   .\scripts\cli.ps1 --bbox 50.72 15.58 50.74 15.62 --circular --json
#
# Override the contact by setting it yourself first:
#   $env:HIKE_OVERPASS_UA = "you@example.com"; .\scripts\cli.ps1 ...
#
# Requires `pip install -e .` (so `hike-finder` is on PATH) in the active venv.
$ErrorActionPreference = 'Stop'
if (-not $env:HIKE_OVERPASS_UA) { $env:HIKE_OVERPASS_UA = 'boikoneov@gmail.com' }
& hike-finder @args
exit $LASTEXITCODE
