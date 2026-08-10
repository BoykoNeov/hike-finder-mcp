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
import os
import sys

from . import cache
from . import config as _config
from .elevation import api_quota_snapshot
from .export import hikes_to_geojson, hikes_to_gpx, pois_to_geojson, pois_to_gpx
from .filters import Criteria
from .format import format_hike, format_poi, format_poi_summary, hike_to_dict
from .poi import kind_labels, normalise_kinds, unrecorded_kinds
from .search import (
    compose_loops,
    compose_loops_around,
    download_area,
    list_area_pois,
    list_snapshot_pois,
    route_via,
    routes_between,
    routes_to_poi,
    search_hikes,
    search_snapshot,
    snapshot_kinds_missing_message,
    snapshot_poi_gap,
)
from .snapshot import (
    default_snapshot_dir,
    list_snapshots,
    load_snapshot,
    save_snapshot,
    snapshot_path,
)


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
        "--transit-access",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="--transit-access = require a train/tram/bus stop near an endpoint (get "
        "there without a car); --no-transit-access = exclude. Rail counts within 1 km, "
        "a tram/bus stop within 400 m. A downloaded area from before this feature has "
        "no transit data and the search will say so rather than guess.",
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
    g.add_argument(
        "--poi",
        action="append",
        metavar="KIND",
        help="Keep only routes that pass a point of interest of this KIND — e.g. "
        "--poi church --poi ruins ('a 10 km hike that goes to a ruin'). Repeat the flag "
        "or give a comma-separated list; several kinds are OR-ed. Each match is reported "
        "with the measured distance. Run --list-poi-kinds for the kinds. Works with a "
        "live, --compose-loops, point-based, or offline --area search. With --show-pois "
        "it selects which kinds to LIST instead of which routes to keep.",
    )
    g.add_argument(
        "--poi-radius",
        type=float,
        metavar="M",
        help="How close a route must pass to count as reaching a --poi, in metres "
        "(default HIKE_POI_RADIUS_M = 250).",
    )

    r = p.add_argument_group(
        "point-based route drawing (each derives its own area — omit --bbox)"
    )
    r.add_argument(
        "--around",
        nargs=2,
        type=float,
        metavar=("LAT", "LON"),
        help="Draw circular day-loops that pass near this point and start there. Loop "
        "length comes from --min-distance/--max-distance (default 3-15 km); how near a "
        "loop must pass is --around-radius. Combine with --car-access/--chairlift-access "
        "to also require a trailhead. Omit --bbox (the area is derived from the point).",
    )
    r.add_argument(
        "--around-radius",
        type=float,
        metavar="M",
        help="How near a loop must pass to the --around point, in metres "
        "(default HIKE_AROUND_RADIUS_M = 1000).",
    )
    r.add_argument(
        "--from",
        dest="from_pt",
        nargs=2,
        type=float,
        metavar=("LAT", "LON"),
        help="Start point: with --to, draw the N shortest routes from here to there.",
    )
    r.add_argument(
        "--to",
        dest="to_pt",
        nargs=2,
        type=float,
        metavar=("LAT", "LON"),
        help="Finish point for --from. Each point is snapped onto the nearest trail.",
    )
    r.add_argument(
        "--to-poi",
        action="append",
        metavar="KIND",
        dest="to_poi",
        help="Destination KIND for --from: draw routes to the nearest church / ruin / peak "
        "instead of to a --to point ('a route to the nearest ruin'). Repeat the flag or give "
        "a comma-separated list; several kinds are OR-ed. Nearest means nearest ALONG THE "
        "TRAILS, and --routes says how many. Run --list-poi-kinds for the kinds. Unlike --poi, "
        "which filters routes by what they pass, this one draws the route to the object.",
    )
    r.add_argument(
        "--to-poi-radius",
        type=float,
        metavar="M",
        help="How far from --from to look for --to-poi destinations, in metres (default "
        "HIKE_POI_SEARCH_RADIUS_M = 3000). It also sizes the fetched area, so raising it "
        "makes the query heavier.",
    )
    r.add_argument(
        "--routes",
        type=int,
        metavar="N",
        help="How many routes to draw: distinct routes between --from and --to, or routes "
        "to the nearest --to-poi destinations. Shortest first (default HIKE_ROUTES_K = 3). "
        "--max-distance caps a route's length.",
    )
    r.add_argument(
        "--via",
        action="append",
        nargs=2,
        type=float,
        metavar=("LAT", "LON"),
        help="Add a waypoint. Repeat it (>=2 times) to draw ONE route linking the points in "
        "the order you give them, each snapped to the nearest trail. Add --via-loop to close "
        "the route into a circular one. Omit --bbox (the area is derived from the points).",
    )
    r.add_argument(
        "--via-loop",
        action="store_true",
        help="With --via points, close the linked route into a circular route back to the "
        "first point, routing the return so it avoids retracing the way out (where the trail "
        "network allows). Points are visited in the order given — no reordering.",
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
        help="Search a snapshot instead of fetching live. No network, no API calls; "
        "--bbox is taken from the snapshot. Takes a path saved by --download, or the "
        "bare NAME of an area shown by --list-areas.",
    )
    s.add_argument(
        "--list-areas",
        action="store_true",
        help="Show which areas are already downloaded — name, bbox, when, and what is in "
        "them — then exit. Lists the NAMED snapshot directory (HIKE_SNAPSHOT_DIR, the one "
        "the web UI downloads into); a snapshot you saved elsewhere with --download PATH "
        "is not tracked, search it with --area PATH.",
    )
    s.add_argument(
        # dest is pinned explicitly: argparse would otherwise derive it from the FIRST
        # long option, renaming args.list_pois out from under run() and the tests.
        "--list-poi-kinds",
        "--list-pois",
        dest="list_pois",
        action="store_true",
        help="List the point-of-interest KINDS --poi / --to-poi / --show-pois accept, "
        "then exit. (To list the actual objects in an area, use --show-pois.)",
    )

    i = p.add_argument_group(
        "browse points of interest (the objects themselves — no routes drawn)"
    )
    i.add_argument(
        "--show-pois",
        action="store_true",
        help="List every point of interest in the area instead of searching for hikes — "
        "e.g. --show-pois --poi ruins,church shows all the ruins and churches in the "
        "--bbox (or in the downloaded --area). No routes are drawn to them and no "
        "elevation is looked up. Omit --poi to show every kind. Combine with --gpx / "
        "--geojson to export them as waypoints for your GPS / phone.",
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


def _split_kinds(values) -> list[str]:
    """Flatten a repeatable, comma-splittable kind flag (``--poi`` / ``--to-poi``).

    Both spellings are supported because both are natural at a shell prompt:
    ``--poi church --poi ruins`` and ``--poi church,ruins``.
    """
    out: list[str] = []
    for item in values or ():
        out.extend(part for part in str(item).split(",") if part.strip())
    return out


def build_criteria(args: argparse.Namespace) -> Criteria:
    # normalise_kinds validates against the registry, so a typo raises here (caught in
    # run()) instead of quietly matching nothing.
    raw_poi = _split_kinds(getattr(args, "poi", None))
    return Criteria(
        min_gain_m=args.min_gain,
        max_gain_m=args.max_gain,
        min_distance_km=args.min_distance,
        max_distance_km=args.max_distance,
        circular=args.circular,
        car_access=args.car_access,
        chairlift_access=args.chairlift_access,
        transit_access=args.transit_access,
        poi_kinds=normalise_kinds(raw_poi),
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


def _write_exports(
    items: list,
    args: argparse.Namespace,
    *,
    gpx=hikes_to_gpx,
    geojson=hikes_to_geojson,
    noun: str = "route(s)",
) -> None:
    """Write the result set to GPX / GeoJSON if --gpx / --geojson were given.

    A side effect alongside the normal stdout rendering (text or --json): the
    confirmation goes to stderr so it never pollutes a --json pipe. An empty result
    still writes a valid (empty) document, so a downstream script always gets a file.

    The serialiser pair is injectable so ``--show-pois`` exports its objects as waypoints
    through this same function — one place that owns "which flag, which file, what to say
    on failure", rather than a near-copy that drifts on the stderr/empty-file contract.
    """
    for path, fn, label in (
        (getattr(args, "gpx", None), gpx, "GPX"),
        (getattr(args, "geojson", None), geojson, "GeoJSON"),
    ):
        if not path:
            continue
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(fn(items))
        except OSError as e:
            print(f"error: could not write {label} to {path!r}: {e}", file=sys.stderr)
            continue
        print(f"Wrote {len(items)} {noun} to {path} ({label}).", file=sys.stderr)


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


_POI_EMPTY = (
    "No routes pass a point of interest of that kind here — widen --poi-radius, try "
    "another --poi kind, or search a wider area. (A miss means nothing of that kind is "
    "*mapped* in OSM near a route, not that nothing is there.)"
)


_SHOW_POI_EMPTY = (
    "No points of interest of that kind are mapped here — pick other --poi kinds (or "
    "drop --poi to show every kind), or look at a wider area. (A miss means nothing of "
    "that kind is *mapped* in OSM here, not that nothing is there.)"
)


def _resolve_area(name: str):
    """Load a saved snapshot by path or by the bare NAME ``--list-areas`` shows.

    Shared by the offline hike search and the offline ``--show-pois`` browse so both
    accept the same spellings. Returns ``(snapshot, None)`` or ``(None, message)``.
    """
    target = name
    if not os.path.isfile(target):
        named = snapshot_path(name)
        if named is not None and named.is_file():
            target = str(named)
    try:
        return load_snapshot(target), None
    except (OSError, ValueError) as e:
        return None, f"error: could not read snapshot {name!r}: {e}"


def _show_pois(args: argparse.Namespace, cfg) -> int:
    """``--show-pois``: list the objects in an area, live or offline, and export them.

    No hikes, no routes, no elevation — see ``search.list_area_pois``. The two sources are
    the live ``--bbox`` and the downloaded ``--area``; both end in the same rendering and
    the same export, so what you browse is exactly what you get in the file.
    """
    kinds = normalise_kinds(_split_kinds(getattr(args, "poi", None)))
    # Flags that describe a WALK have nothing to act on here, and a filter that silently
    # does nothing is the one outcome this project's conventions forbid. Not an error —
    # they are plausibly left over from the previous command — but never silent either.
    ignored = [
        flag
        for flag, value in (
            ("--min-gain", args.min_gain), ("--max-gain", args.max_gain),
            ("--min-distance", args.min_distance), ("--max-distance", args.max_distance),
            ("--circular", args.circular), ("--car-access", args.car_access),
            ("--chairlift-access", args.chairlift_access),
            ("--poi-radius", getattr(args, "poi_radius", None)),
            ("--near-misses", args.near_misses),
            ("--name-places", args.name_places),
        )
        if value is not None
    ]
    if ignored:
        print(
            f"note: --show-pois lists objects, not walks — {', '.join(ignored)} "
            f"do not apply and were ignored.",
            file=sys.stderr,
        )
    empty_msg = _SHOW_POI_EMPTY
    if args.area:
        snap, err = _resolve_area(args.area)
        if snap is None:
            print(err, file=sys.stderr)
            return 1
        places = list_snapshot_pois(snap, kinds)
        # "This area has no ruins" and "this file cannot say" are different answers with
        # different fixes, and only one of them is about the landscape. --list-areas
        # already flags such a snapshot; this is the same distinction at the point where
        # it would otherwise be lost. `snapshot_poi_gap` owns which of the four cases
        # holds, so the CLI only decides how to word it.
        state, gap = snapshot_poi_gap(snap, kinds)
        if state == "none":
            empty_msg = (
                f"This downloaded area carries no points of interest — it was saved "
                f"before the feature existed. Re-download it to browse and export them "
                f"offline (hike-finder --bbox … --download {args.area})."
            )
        elif state == "missing":
            # Said even when the listing is NON-empty: asking for ruins and trees against
            # a file that never looked for trees returns a list of ruins, and printed
            # bare it reads as the whole answer.
            print(
                f"note: {snapshot_kinds_missing_message(gap or ())} "
                f"(hike-finder --bbox … --download {args.area})",
                file=sys.stderr,
            )
            # `empty_msg` deliberately stays at _SHOW_POI_EMPTY: the note above already
            # names the kinds nobody looked for, and it prints whether or not the listing
            # came back empty.
        elif state == "unknown":
            empty_msg = (
                f"This downloaded area does not record which kinds it was saved with, so "
                f"an empty result here cannot be told apart from a kind nobody looked "
                f"for. Re-download it if you expected something "
                f"(hike-finder --bbox … --download {args.area})."
            )
    else:
        if not args.bbox:
            print(
                "error: --show-pois needs --bbox (or --area FILE to browse a downloaded "
                "area).",
                file=sys.stderr,
            )
            return 2
        try:
            places = list_area_pois(
                tuple(args.bbox),
                kinds,
                cfg,
                user_agent=args.user_agent,
                overpass_url=args.overpass_url,
            )
        except Exception as e:  # network/HTTP errors surface here
            _fetch_hint(e)
            return 1

    if args.json:
        print(json.dumps([p.to_dict() for p in places], ensure_ascii=False, indent=2))
    elif not places:
        print(empty_msg)
    else:
        # Say what the mix is before ninety lines of it — the first question about a long
        # list is always its composition, and the summary answers it in one line.
        print(format_poi_summary(places))
        for p in places:
            print(f"  {format_poi(p)}")
    _write_exports(
        list(places), args, gpx=pois_to_gpx, geojson=pois_to_geojson,
        noun="point(s) of interest",
    )
    return 0


def _print_areas(as_json: bool) -> int:
    """Show the already-downloaded areas (the named snapshot directory)."""
    areas = list_snapshots()
    if as_json:
        print(json.dumps(areas, ensure_ascii=False, indent=2))
        return 0
    if not areas:
        print(
            f"No downloaded areas in {default_snapshot_dir()}.\n"
            "Download one with the web UI, or search a file saved by --download FILE "
            "using --area FILE."
        )
        return 0
    print(f"Downloaded areas in {default_snapshot_dir()}:")
    for a in areas:
        bbox = a.get("bbox") or []
        where = (
            f"{bbox[0]:.4f},{bbox[1]:.4f} .. {bbox[2]:.4f},{bbox[3]:.4f}"
            if len(bbox) == 4
            else "bbox unknown"
        )
        when = (a.get("created_at") or "?").replace("T", " ").replace("+00:00", "Z")
        # An older snapshot has no POIs, so say so here rather than letting a --poi
        # search against it look like "there are no churches in Krkonoše".
        poi = f"{a['pois']} POIs" if a.get("pois") else "no POIs (re-download for --poi)"
        # The finer version of the same point: a file can be full of POIs and still
        # predate whole kinds. The inventory is where someone decides which area to
        # search, so it is the cheapest place to learn the file is behind the registry.
        recorded = a.get("poi_kinds")
        if a.get("pois"):
            behind = unrecorded_kinds(tuple(recorded) if recorded is not None else None)
            if behind is None:
                poi += ", kinds not recorded"
            elif behind:
                poi += f", {len(behind)} kind(s) newer than it"
        names = f", {a['places']} baked names" if a.get("places") else ""
        print(
            f"  {a['name']:<20} {where}\n"
            f"  {'':<20} {a['routes']} routes, {a['samples']} elevation samples, "
            f"{poi}{names}  ·  {a['bytes'] / 1e6:.1f} MB, downloaded {when}"
        )
    print("\nSearch one with:  hike-finder --area <name>   (or --area <path/to/file.json>)")
    return 0


def run(args: argparse.Namespace) -> int:
    cfg = _config.load()
    near_miss = "auto" if args.near_misses is None else args.near_misses

    # Standalone informational actions: print and exit, no network, no bbox needed.
    if getattr(args, "list_pois", False):
        print("Point-of-interest kinds for --poi, --to-poi and --show-pois (OR-ed):")
        for kind, plural in kind_labels():
            print(f"  {kind:<14} {plural}")
        print(
            "\n--poi FILTERS routes by what they pass; --to-poi DRAWS a route to the "
            "nearest one;\n--show-pois LISTS the objects themselves, with no routes at "
            "all.\n"
            "\ne.g.  hike-finder --bbox 50.72 15.58 50.78 15.68 --poi ruins,church "
            "--max-distance 12"
            "\n      hike-finder --from 50.7312 15.6044 --to-poi ruins --routes 2"
            "\n      hike-finder --bbox 50.72 15.58 50.78 15.68 --show-pois --poi ruins "
            "--gpx ruins.gpx"
        )
        return 0
    if getattr(args, "list_areas", False):
        return _print_areas(args.json)

    # Validate --poi / --to-poi once, up front: an unknown kind is a typo, and the whole
    # point of raising is that it must not read as "nothing of that kind is out there".
    try:
        build_criteria(args)
        to_poi = normalise_kinds(_split_kinds(getattr(args, "to_poi", None)))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if getattr(args, "poi_radius", None) is not None:
        cfg.poi_radius_m = args.poi_radius

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

    # --show-pois is a different question ("what objects are here?"), not a hike search
    # with a filter on it, so it is answered here — before every mode below, each of
    # which would otherwise silently ignore it. The incompatible combinations are named
    # one by one rather than lumped together, because each is a different mistake.
    if getattr(args, "show_pois", False):
        if args.download:
            print(
                "error: --show-pois lists an area's objects; --download saves a snapshot. "
                "Download first, then browse it with --show-pois --area FILE.",
                file=sys.stderr,
            )
            return 2
        if getattr(args, "compose_loops", False):
            print(
                "error: --show-pois draws no routes at all, so there is nothing for "
                "--compose-loops to compose.",
                file=sys.stderr,
            )
            return 2
        if (
            getattr(args, "around", None) is not None
            or getattr(args, "from_pt", None) is not None
            or getattr(args, "to_pt", None) is not None
            or to_poi
            or getattr(args, "via", None) is not None
            or getattr(args, "via_loop", False)
        ):
            print(
                "error: --show-pois lists objects without routing to them — drop the "
                "point-based flags, or use --from … --to-poi KIND to draw a route to the "
                "nearest one instead.",
                file=sys.stderr,
            )
            return 2
        return _show_pois(args, cfg)

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

    # Point-based route drawing: --around (circular routes near a point), --from/--to (N
    # shortest routes between two points), --from/--to-poi (routes to the nearest church /
    # ruin / peak), and --via (one route linking several points, closed into a circular route
    # with --via-loop). Each derives its own area, so none takes --bbox or combines with
    # --compose-loops / --area / --download.
    around = getattr(args, "around", None)
    from_pt = getattr(args, "from_pt", None)
    to_pt = getattr(args, "to_pt", None)
    via = getattr(args, "via", None)
    via_loop = getattr(args, "via_loop", False)
    if (
        around is not None or from_pt is not None or to_pt is not None
        or to_poi or via is not None or via_loop
    ):
        # --from takes exactly one destination: a place (--to) or a kind of object
        # (--to-poi). Each half is checked separately so the error names the actual mistake.
        if to_pt is not None and to_poi:
            print(
                "error: --to and --to-poi are two different destinations (a point vs the "
                "nearest object of a kind) — use one, not both.",
                file=sys.stderr,
            )
            return 2
        if (to_pt is not None or to_poi) and from_pt is None:
            print(
                "error: --to / --to-poi need a --from start point.", file=sys.stderr
            )
            return 2
        if from_pt is not None and to_pt is None and not to_poi:
            print(
                "error: --from needs a destination — give --to LAT LON, or --to-poi KIND "
                "to route to the nearest object of that kind.",
                file=sys.stderr,
            )
            return 2
        active = sum(
            1
            for on in (
                around is not None,
                from_pt is not None or to_pt is not None or bool(to_poi),
                via is not None,
            )
            if on
        )
        if active > 1:
            print(
                "error: --around, --from/--to(-poi) and --via are different point-based "
                "modes — use one, not several.",
                file=sys.stderr,
            )
            return 2
        if via is not None and len(via) < 2:
            print(
                "error: --via needs at least two points — repeat --via LAT LON.",
                file=sys.stderr,
            )
            return 2
        if via_loop and via is None:
            print("error: --via-loop only applies with --via points.", file=sys.stderr)
            return 2
        if args.area or args.download:
            print(
                "error: point-based modes are live searches; they can't be combined with "
                "--area or --download.",
                file=sys.stderr,
            )
            return 2
        if getattr(args, "compose_loops", False):
            print(
                "error: point-based modes already synthesise routes; drop --compose-loops.",
                file=sys.stderr,
            )
            return 2
        if args.bbox:
            print(
                "error: point-based modes derive their own area from the point(s); omit --bbox.",
                file=sys.stderr,
            )
            return 2
        used_before, _ = api_quota_snapshot(cfg)
        common = dict(
            user_agent=args.user_agent,
            overpass_url=args.overpass_url,
            elevation_mode=args.elevation_mode,
            dem_dir=args.dem_dir,
        )
        try:
            if around is not None:
                hikes = compose_loops_around(
                    (around[0], around[1]),
                    build_criteria(args),
                    cfg,
                    radius_m=args.around_radius,
                    near_miss=near_miss,
                    **common,
                )
                empty_msg = (
                    "No circular routes pass within the radius of your point — widen "
                    "--around-radius, the --min-distance/--max-distance band, or drop "
                    "--car-access/--chairlift-access."
                )
            elif via is not None:
                hikes = route_via(
                    [(lat, lon) for lat, lon in via],
                    build_criteria(args),
                    cfg,
                    loop=via_loop,
                    **common,
                )
                empty_msg = (
                    "No circular route could be drawn through your points — a point may be "
                    "off-network (>~2 km from any trail) or a leg crosses a gap; move them "
                    "onto/closer to marked trails."
                    if via_loop
                    else "No route could be drawn through your points — a point may be "
                    "off-network (>~2 km from any trail), a leg crosses a gap in the trail "
                    "network, or the linked route falls outside --min/--max-distance."
                )
            elif to_poi:
                hikes = routes_to_poi(
                    (from_pt[0], from_pt[1]),
                    to_poi,
                    build_criteria(args),
                    cfg,
                    n=args.routes,
                    search_radius_m=args.to_poi_radius,
                    **common,
                )
                # Destination-shaped, NOT the area-filter wording of --poi: nothing here was
                # "filtered out of an area", a route to an object could not be drawn. The
                # three causes need three different fixes, and the mode logs which one it hit
                # — this line points at that.
                empty_msg = (
                    "No route could be drawn to an object of that kind — either nothing of "
                    "it is mapped within the search radius (widen --to-poi-radius), the ones "
                    "found sit off the trail network, or every route to them runs past the "
                    "length cap (raise --max-distance). The line logged above says which. "
                    "(A miss means nothing of that kind is *mapped* in OSM near your point, "
                    "not that nothing is there.)"
                )
            else:
                hikes = routes_between(
                    (from_pt[0], from_pt[1]),
                    (to_pt[0], to_pt[1]),
                    build_criteria(args),
                    cfg,
                    k=args.routes,
                    **common,
                )
                empty_msg = (
                    "No routes could be drawn between your two points — they may sit on "
                    "disconnected trail networks, or every route exceeds the length cap "
                    "(--max-distance)."
                )
        except Exception as e:  # network/HTTP/elevation errors surface here
            _fetch_hint(e)
            return 1
        _quota_line(cfg, used_before)
        _emit(hikes, args.json, empty_msg)
        _write_exports(hikes, args)
        return 0

    # Offline: search a saved snapshot. No network, no API calls, no quota line.
    if args.area:
        # A path wins; otherwise fall back to the NAMED snapshot directory, so the names
        # --list-areas (and the web UI) show are usable here verbatim.
        snap, err = _resolve_area(args.area)
        if snap is None:
            print(err, file=sys.stderr)
            return 1
        criteria = build_criteria(args)
        hikes = search_snapshot(
            snap, criteria, cfg, near_miss=near_miss, name_places=args.name_places,
        )
        # A POI filter that finds nothing offline gets the same "here's the lever" wording
        # as the live search — plus, from search_snapshot, a loud warning when the
        # snapshot simply predates POIs and could never have matched.
        _emit(
            hikes, args.json,
            _POI_EMPTY if criteria.poi_kinds else "No matching hikes found in that area.",
        )
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
            f"{snap.sample_count} elevation samples, {snap.poi_count} points of "
            f"interest{baked}. Search it offline with --area {args.download}."
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
    elif build_criteria(args).poi_kinds:
        # Say which lever to pull: with a POI filter on, "nothing matched" most often
        # means the radius is too tight or nothing of that kind is mapped here — not
        # that the distance/gain band was wrong.
        empty_msg = _POI_EMPTY
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
