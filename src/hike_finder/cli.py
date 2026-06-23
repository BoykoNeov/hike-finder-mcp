"""Standalone command-line interface — find hikes with no MCP client or LLM.

Same engine as the MCP server (overpass + filters), just a plain terminal
frontend. Example::

    hike-finder --bbox 50.72 15.58 50.74 15.62 --circular --chairlift-access \\
                --user-agent you@example.com

Bounding-box order is ``south west north east`` (min-lat min-lon max-lat max-lon).
The three boolean filters are tri-state: omit = don't care, ``--circular`` =
require, ``--no-circular`` = exclude.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import config as _config
from .elevation import api_quota_snapshot
from .filters import Criteria
from .format import format_hike, hike_to_dict
from .search import search_hikes


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hike-finder",
        description=(
            "Find marked OSM hiking routes in a bounding box, with locally computed "
            "elevation gain/distance plus shape and access filters. No LLM or MCP "
            "client required."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        required=True,
        metavar=("SOUTH", "WEST", "NORTH", "EAST"),
        help="Bounding box: min-lat min-lon max-lat max-lon (e.g. openstreetmap.org Export tab).",
    )

    g = p.add_argument_group("filters (all optional)")
    g.add_argument("--min-gain", type=float, metavar="M", help="Minimum elevation gain, metres.")
    g.add_argument("--max-gain", type=float, metavar="M", help="Maximum elevation gain, metres.")
    g.add_argument("--min-distance", type=float, metavar="KM", help="Minimum route length, km.")
    g.add_argument("--max-distance", type=float, metavar="KM", help="Maximum route length, km.")
    g.add_argument(
        "--circular",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="--circular = loops only; --no-circular = point-to-point only.",
    )
    g.add_argument(
        "--car-access",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="--car-access = require parking near an endpoint; --no-car-access = exclude.",
    )
    g.add_argument(
        "--chairlift-access",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="--chairlift-access = require a ride-up lift near an endpoint; --no-chairlift-access = exclude.",
    )

    o = p.add_argument_group("data sources (override env / config defaults)")
    o.add_argument(
        "--user-agent",
        help="Overpass User-Agent contact, overrides HIKE_OVERPASS_UA. The public "
        "server rejects the default Python User-Agent (406); use a real email/URL.",
    )
    o.add_argument("--overpass-url", help="Overpass endpoint, overrides HIKE_OVERPASS_URL.")
    o.add_argument(
        "--elevation-mode",
        choices=("api", "local", "auto"),
        help="Elevation backend, overrides HIKE_ELEVATION_MODE.",
    )
    o.add_argument("--dem-dir", help="GeoTIFF DEM tile directory for local/auto, overrides HIKE_DEM_DIR.")

    p.add_argument("--json", action="store_true", help="Emit results as JSON instead of text lines.")
    return p


def build_criteria(args: argparse.Namespace) -> Criteria:
    return Criteria(
        min_gain_m=args.min_gain,
        max_gain_m=args.max_gain,
        min_distance_km=args.min_distance,
        max_distance_km=args.max_distance,
        circular=args.circular,
        car_access=args.car_access,
        chairlift_access=args.chairlift_access,
    )


def run(args: argparse.Namespace) -> int:
    bbox = tuple(args.bbox)  # (south, west, north, east)
    cfg = _config.load()
    try:
        hikes = search_hikes(
            bbox,
            build_criteria(args),
            cfg=cfg,
            user_agent=args.user_agent,
            overpass_url=args.overpass_url,
            elevation_mode=args.elevation_mode,
            dem_dir=args.dem_dir,
        )
    except Exception as e:  # network/HTTP/elevation errors surface here
        print(f"error: failed to fetch hikes: {e}", file=sys.stderr)
        if "406" in str(e):
            print(
                "hint: set a real contact with --user-agent or HIKE_OVERPASS_UA — the "
                "public Overpass server rejects the default User-Agent.",
                file=sys.stderr,
            )
        return 1

    # Show how close we are to the elevation API's daily cap (stderr, so --json
    # stdout stays clean). Skip in local-DEM mode — the API isn't used there, so a
    # quota line would just be confusing.
    mode = args.elevation_mode or cfg.elevation_mode
    if mode != "local":
        used, limit = api_quota_snapshot(cfg)
        if limit > 0:
            print(
                f"elevation API: {used}/{limit} requests used today "
                f"({max(0, limit - used)} remaining, resets at UTC midnight)",
                file=sys.stderr,
            )

    if args.json:
        print(json.dumps([hike_to_dict(h) for h in hikes], ensure_ascii=False, indent=2))
        return 0
    if not hikes:
        print("No matching hikes found in that area.")
        return 0
    for h in hikes:
        print(format_hike(h))
    return 0


def main(argv: list[str] | None = None) -> None:
    # Route names are often non-ASCII (Czech KČT trails: "Špindlmanova mise") and
    # the summary uses an em-dash. On Windows the console defaults to cp1252, which
    # can't encode them and would crash on print — force UTF-8, degrade if it can't.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    main()
