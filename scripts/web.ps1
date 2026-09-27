# hike-finder Web UI launcher (Windows PowerShell / pwsh).
#
# Thin wrapper over the `hike-finder-web` entry point: starts the local map
# server. This is long-running — press Ctrl+C to stop. Extra args (e.g. --host /
# --port / --open) are forwarded; the server prints its own URL to stdout.
#
#   .\scripts\web.ps1                 # http://127.0.0.1:8765
#   .\scripts\web.ps1 --port 9000
#
# Set your own contact first — the OpenStreetMap servers ask every program for
# one, and this repo ships none (without it a generic identifier is sent):
#   $env:HIKE_OVERPASS_UA = "you@example.com"; .\scripts\web.ps1
# (or type it into the page's Contact box). No terminal? Double-click
# start-hike-finder.cmd in the repo root instead — it asks for it.
#
# Requires `pip install -e .` (so `hike-finder-web` is on PATH) in the active venv.
$ErrorActionPreference = 'Stop'
& hike-finder-web @args
exit $LASTEXITCODE
