"""Pin the per-interface launcher scripts in ``scripts/``.

Each launcher is a THIN wrapper: it sets a default Overpass contact (only when
unset) and forwards to the matching pyproject entry point —

  scripts/cli.{sh,ps1}  -> hike-finder        (one-shot; results on stdout)
  scripts/web.{sh,ps1}  -> hike-finder-web     (long-running map server)
  scripts/mcp.{sh,ps1}  -> hike-finder-mcp     (stdio JSON-RPC; stdout MUST stay clean)

How each is pinned, matched to its shape:

  - CLI/web wrappers: forward ``--help``. argparse exits 0 and the usage text
    comes from the REAL entry point, proving the wrapper reached it (and that a
    default contact env-var doesn't get in the way).
  - MCP wrapper: a REAL stdio MCP handshake (initialize + list_tools) against
    the launcher, exactly like ``test_server.py`` pins the server itself. A
    passing handshake is the load-bearing check that the wrapper wrote NOTHING
    to stdout — any banner/echo there would corrupt the JSON-RPC stream and the
    handshake would fail. ``list_tools`` touches no network.

Every test skips cleanly when its interpreter (bash / powershell) or the
pip-installed entry point isn't available, so a fresh, uninstalled checkout — or
a CI box without that shell — doesn't fail; it just doesn't exercise that flavour.
"""
import asyncio
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = _ROOT / "scripts"
SRC = str(_ROOT / "src")

# Both flavours are attempted on every OS; the interpreter guard below skips the
# one whose shell isn't installed (e.g. .ps1 on a Linux box without pwsh).
FLAVOURS = (".sh", ".ps1")


def _usable(interp: str, probe: list[str]) -> bool:
    """True only if ``interp`` actually *runs*. ``shutil.which`` can find a shim
    that is present-yet-broken — e.g. a stale WSL ``bash`` relay whose distro is
    gone answers ``which`` but dies with ``execvpe(/bin/bash) failed`` on exec.
    A dead relay may hang rather than exit non-zero, so the probe is bounded."""
    try:
        proc = subprocess.run(
            [interp, *probe], capture_output=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _runner_for(script: Path):
    """Return (interpreter, prefix_args) to run ``script``, or None if the
    interpreter isn't on this machine (or is present-yet-broken)."""
    if script.suffix == ".sh":
        bash = shutil.which("bash")
        if bash and _usable(bash, ["-c", "exit 0"]):
            return (bash, [])
        return None
    if script.suffix == ".ps1":
        ps = shutil.which("pwsh") or shutil.which("powershell")
        if ps and _usable(ps, ["-NoProfile", "-Command", "exit 0"]):
            return (ps, ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        return None
    return None


def _resolve(name: str, suffix: str, entry: str):
    """Common skip-or-go preamble: (script_path, interpreter, prefix_args)."""
    script = SCRIPTS / f"{name}{suffix}"
    if not script.exists():
        pytest.skip(f"{script.name} missing")
    if shutil.which(entry) is None:
        pytest.skip(f"{entry} not on PATH — run `pip install -e .` first")
    runner = _runner_for(script)
    if runner is None:
        pytest.skip(f"no interpreter available for {suffix}")
    return script, runner[0], runner[1]


@pytest.mark.parametrize("suffix", FLAVOURS)
@pytest.mark.parametrize("name,entry", [("cli", "hike-finder"), ("web", "hike-finder-web")])
def test_cli_web_launcher_forwards_help(name, entry, suffix):
    script, interp, prefix = _resolve(name, suffix, entry)
    proc = subprocess.run(
        [interp, *prefix, str(script), "--help"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in proc.stdout.lower()  # came from the real entry point


@pytest.mark.parametrize("suffix", FLAVOURS)
def test_mcp_launcher_keeps_stdout_clean(suffix):
    pytest.importorskip("mcp")  # optional extra; skip if absent
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import get_default_environment, stdio_client

    script, interp, prefix = _resolve("mcp", suffix, "hike-finder-mcp")
    params = StdioServerParameters(
        command=interp,
        args=[*prefix, str(script)],
        # Extend (not replace) the safe default env so Windows keeps SystemRoot/
        # PATH; PYTHONPATH points at src so the child imports the package whether
        # or not it's pip-installed.
        env={**get_default_environment(), "PYTHONPATH": SRC},
    )

    async def _impl():
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=30)
            ) as session:
                await session.initialize()
                return await session.list_tools()

    result = asyncio.run(asyncio.wait_for(_impl(), timeout=60))
    assert {t.name for t in result.tools} == {
        "find_hikes", "circular_routes", "routes_between", "download_area"
    }
