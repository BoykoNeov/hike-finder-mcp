"""Run the web UI's browser JavaScript under node, against a stubbed Leaflet + DOM.

``web.INDEX_HTML`` carries a real Leaflet application: the drawn-box selection that the
download AND the search both read, the downloaded-areas panel, and the points-of-interest
filter. The Python tests cover the HTTP endpoints below it, but none of that client logic
— so this extracts the page's script and drives it through ``tests/webui_harness.cjs``,
which asserts on the behaviour a user would see.

Skipped (not failed) when node is unavailable, mirroring how ``test_launchers.py`` skips
its ``.sh`` cases without bash: node is a convenience for checking this layer, never a
dependency of the project.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from hike_finder import web

HARNESS = Path(__file__).parent / "webui_harness.cjs"


def _page_script() -> str:
    """The page's own inline <script> (not the Leaflet CDN tag)."""
    scripts = re.findall(
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", web.INDEX_HTML, re.S
    )
    assert len(scripts) == 1, f"expected exactly one inline script, found {len(scripts)}"
    return scripts[0]


def test_index_html_has_exactly_one_inline_script():
    """Runs without node: pins the shape the harness (and this extraction) relies on."""
    assert "L.map(" in _page_script()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_web_ui_javascript_behaves(tmp_path):
    page = tmp_path / "page.js"
    page.write_text(_page_script(), encoding="utf-8")

    # `encoding=` is not decoration on either call: the page is full of Czech names, em
    # dashes and curly quotes, and a FAILING check echoes the offending HTML back. Under
    # Windows' default cp1252 that decode raises, so the harness's own report would come
    # back as a UnicodeDecodeError instead of as the assertion it is meant to be — a test
    # that hides its result the moment it has one worth showing.
    #
    # Syntax first, so a parse error reports as one rather than as a runtime mess.
    syntax = subprocess.run(
        ["node", "--check", str(page)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert syntax.returncode == 0, syntax.stderr

    result = subprocess.run(
        ["node", str(HARNESS), str(page)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL 0" in result.stdout, result.stdout
