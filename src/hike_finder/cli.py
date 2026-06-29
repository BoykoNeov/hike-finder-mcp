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

from . import cache
from . import config as _config
from .elevation import api_quota_snapshot
from .export import hikes_to_geojson, hikes_to_gpx
from .filters import Criteria
from .format import format_hike, hike_to_dict
from .search import compose_loops, download_area, search_hikes, search_snapshot
from .snapshot import load_snapshot, save_snapshot


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
        metavar=("SOUTH", "WEST", "NORTH", "EAST"),
        help="Bounding box: min-lat min-lon max-lat max-lon (e.g. openstreetmap.org Export tab). "
        "Required unless --area is given.",
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
    g.add_argument(
        "--near-misses",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Also list routes that just miss the filters, annotated with how (e.g. "
        "gain 80 m short). Default: shown only when nothing matches. --near-misses = "
        "always show; --no-near-misses = never.",
    )
    g.add_argument(
        "--compose-loops",
        action="store_true",
        help="Synthesise loops by combining connected marked trails, instead of "
        "reporting each OSM relation as-is — finds day-loops that aren't mapped as a "
        "single relation. Target length comes from --min-distance/--max-distance "
        "(default 3-15 km). Each result is stitched from several trails (shown as "
        "'composed of ...'). Loops are kept inside the --bbox area. Combine with "
        "--car-access / --chairlift-access to get only loops reachable from a parking "
        "lot / lift, each started at that trailhead ('a loop from where I park').",
    )

    s = p.add_argument_group("saved areas (fetch once, then search offline)")
    s.add_argument(
        "--download",
        metavar="FILE",
        help="Fetch the --bbox area (routes + elevation for every plausible route) and "
        "save it to FILE. Spends the elevation budget once; afterwards search FILE with "
        "--area and no network is used.",
    )
    s.add_argument(
        "--area",
        metavar="FILE",
        help="Search a snapshot saved by --download instead of fetching live. No network, "
        "no API calls; --bbox is taken from the snapshot.",
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
    o.add_argument(
        "--name-places",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Reverse-geocode UNNAMED routes (route/<id>) to a place-derived label "
        "(e.g. 'Pec → Sněžka') via Nominatim. Off by default (also HIKE_GEOCODE); only "
        "the matched routes are looked up, throttled and cached. No effect offline "
        "(--area needs the network).",
    )
    o.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the on-disk cache for this run (always re-fetch Overpass + "
        "elevation). The cache is on by default; also disable via HIKE_CACHE=0.",
    )
    o.add_argument(
        "--clear-cache",
        action="store_true",
        help="Empty the on-disk cache (Overpass areas + elevation points) and exit.",
    )

    x = p.add_argument_group("export (write the routes to a file you can load into a GPS / phone)")
    x.add_argument(
        "--gpx",
        metavar="FILE",
        help="Also write the matched + composed routes to FILE as GPX 1.1 (one track per "
        "route plus a start waypoint) — load into Komoot / OsmAnd / Garmin / mapy.cz. "
        "Works with a live, --compose-loops, or offline --area search; the text/--json "
        "output is still printed.",
    )
    x.add_argument(
        "--geojson",
        metavar="FILE",
        help="Also write the matched + composed routes to FILE as GeoJSON (a "
        "FeatureCollection of route lines carrying the full computed stats).",
    )

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


def _emit(hikes: list, as_json: bool, empty_msg: str = "No matching hikes found in that area.") -> None:
    """Print results: JSON array, or one text line per hike (near-misses flagged)."""
    if as_json:
        print(json.dumps([hike_to_dict(h) for h in hikes], ensure_ascii=False, indent=2))
        return
    if not hikes:
        print(empty_msg)
        return
    for h in hikes:
        print(format_hike(h))


def _write_exports(hikes: list, args: argparse.Namespace) -> None:
    """Write the result set to GPX / GeoJSON if --gpx / --geojson were given.

    A side effect alongside the normal stdout rendering (text or --json): the
    confirmation goes to stderr so it never pollutes a --json pipe. An empty result
    still writes a valid (empty) document, so a downstream script always gets a file.
    """
    for path, fn, label in (
        (getattr(args, "gpx", None), hikes_to_gpx, "GPX"),
        (getattr(args, "geojson", None), hikes_to_geojson, "GeoJSON"),
    ):
        if not path:
            continue
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(fn(hikes))
        except OSError as e:
            print(f"error: could not write {label} to {path!r}: {e}", file=sys.stderr)
            continue
        print(f"Wrote {len(hikes)} route(s) to {path} ({label}).", file=sys.stderr)


def _quota_line(cfg, used_before: int) -> None:
    """Show progress against the elevation API's daily cap, but only when the API was
    actually hit this run (the counter moved) — a local-DEM `auto` run stays silent."""
    used, limit = api_quota_snapshot(cfg)
    if limit > 0 and used > used_before:
        print(
            f"elevation API: {used}/{limit} requests used today "
            f"({max(0, limit - used)} remaining, resets at UTC midnight)",
            file=sys.stderr,
        )


def _fetch_hint(e: Exception) -> None:
    print(f"error: failed to fetch hikes: {e}", file=sys.stderr)
    if "406" in str(e):
        print(
            "hint: set a real contact with --user-agent or HIKE_OVERPASS_UA — the "
            "public Overpass server rejects the default User-Agent.",
            file=sys.stderr,
        )


def run(args: argparse.Namespace) -> int:
    cfg = _config.load()
    near_miss = "auto" if args.near_misses is None else args.near_misses

    # --clear-cache is a standalone maintenance action: empty the cache and exit.
    if getattr(args, "clear_cache", False):
        c = cache.Cache(cache.cache_path_from_config(cfg))
        c.clear()
        print(f"Cleared cache at {cache.cache_path_from_config(cfg)}.")
        return 0

    # --no-cache disables the transparent cache for this whole run.
    if getattr(args, "no_cache", False):
        cfg.cache_enabled = False

    if args.area and args.download:
        print("error: --area and --download are mutually exclusive.", file=sys.stderr)
        return 2

    if getattr(args, "compose_loops", False) and (args.area or args.download):
        print(
            "error: --compose-loops is a live search; it can't be combined with "
            "--area or --download.",
            file=sys.stderr,
        )
        return 2

    if (getattr(args, "gpx", None) or getattr(args, "geojson", None)) and args.download:
        print(
            "error: --gpx/--geojson export the search results; they can't be combined "
            "with --download (which writes a snapshot, not routes).",
            file=sys.stderr,
        )
        return 2

    # Offline: search a saved snapshot. No network, no API calls, no quota line.
    if args.area:
        try:
            snap = load_snapshot(args.area)
        except (OSError, ValueError) as e:
            print(f"error: could not read snapshot {args.area!r}: {e}", file=sys.stderr)
            return 1
        hikes = search_snapshot(
            snap, build_criteria(args), cfg, near_miss=near_miss,
            name_places=args.name_places,
        )
        _emit(hikes, args.json)
        _write_exports(hikes, args)
        return 0

    if not args.bbox:
        print("error: --bbox is required (or pass --area FILE to search a snapshot).", file=sys.stderr)
        return 2
    bbox = tuple(args.bbox)  # (south, west, north, east)

    # Download: fetch the area + warm elevation for every plausible route, save to file.
    # With --name-places it also bakes reverse-geocoded names for the unnamed routes, so
    # the later offline --area search can label them with zero network.
    if args.download:
        used_before, _ = api_quota_snapshot(cfg)
        try:
            snap = download_area(
                bbox,
                cfg=cfg,
                user_agent=args.user_agent,
                overpass_url=args.overpass_url,
                elevation_mode=args.elevation_mode,
                dem_dir=args.dem_dir,
                name_places=args.name_places,
            )
        except Exception as e:  # network/HTTP errors surface here
            _fetch_hint(e)
            return 1
        try:
            save_snapshot(snap, args.download)
        except OSError as e:
            print(f"error: could not write snapshot {args.download!r}: {e}", file=sys.stderr)
            return 1
        baked = f", {snap.place_count} baked place name(s)" if args.name_places else ""
        print(
            f"Saved snapshot to {args.download}: {snap.route_count} routes, "
            f"{snap.sample_count} elevation samples{baked}. "
            f"Search it offline with --area {args.download}."
        )
        _quota_line(cfg, used_before)
        return 0

    # Live search. --compose-loops swaps in the loop-composition engine; everything
    # else (rendering, quota line, error handling) is shared.
    composing = getattr(args, "compose_loops", False)
    search = compose_loops if composing else search_hikes
    used_before, _ = api_quota_snapshot(cfg)
    kwargs = dict(
        cfg=cfg,
        user_agent=args.user_agent,
        overpass_url=args.overpass_url,
        elevation_mode=args.elevation_mode,
        dem_dir=args.dem_dir,
        near_miss=near_miss,
    )
    # Reverse-geocode naming only applies to ordinary routes — a composed loop is
    # already labelled by its constituent trails ("composed of …"), never route/<id>.
    if not composing:
        kwargs["name_places"] = args.name_places
    try:
        hikes = search(bbox, build_criteria(args), **kwargs)
    except Exception as e:  # network/HTTP/elevation errors surface here
        _fetch_hint(e)
        return 1

    _quota_line(cfg, used_before)
    if getattr(args, "compose_loops", False):
        # When access is required, an empty result may mean "loops exist but none come near
        # a parking/lift" rather than "no loops at all" — say so, so the filter isn't silent.
        anchored = args.car_access is True or args.chairlift_access is True
        empty_msg = (
            "No loops could be composed reachable from a parking lot / lift in that area "
            "— drop --car-access/--chairlift-access, or try a wider --bbox or distance band."
            if anchored
            else "No loops could be composed in that area — try a wider --bbox or a wider "
            "--min-distance/--max-distance band."
        )
    else:
        empty_msg = "No matching hikes found in that area."
    _emit(hikes, args.json, empty_msg)
    _write_exports(hikes, args)
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
