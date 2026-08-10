"""Area snapshots: fetch an area once, then search it offline with zero API calls.

The expensive parts of a search are the network: ONE Overpass call for the routes/
parking/lifts, and MANY elevation-API calls for the per-point gain/loss. A snapshot
captures both — the raw :class:`~hike_finder.overpass.AreaData` plus every elevation
sample that was looked up — into a single JSON file. Afterwards a search against the
snapshot touches no network at all: routes are re-stitched from the saved geometry and
elevation is answered from the saved samples.

Two providers do the bridging, both honouring the plain
:class:`~hike_finder.elevation.base.ElevationProvider` ``lookup`` contract so the
unchanged two-pass filter (``filters.find_hikes``) drives them exactly as it drives the
live API — offline results are therefore identical to online *by construction*, not by a
parallel code path:

  * :class:`RecordingElevationProvider` wraps the real provider during a download and
    remembers every ``point -> elevation`` it returns.
  * :class:`SnapshotElevationProvider` answers later from that recording.

The *same* seam bakes reverse-geocoded place names (``naming.py``) into a snapshot so
an offline ``--area`` search can label its unnamed routes with zero network — opt-in at
download time, mirroring the elevation pair:

  * :class:`RecordingGeocoder` wraps the real geocoder during a (name-baking) download
    and remembers every ``point -> place`` it resolves.
  * :class:`SnapshotGeocoder` answers later from that recording, driven by the unchanged
    ``naming.enrich_names`` exactly as the live ``NominatimGeocoder`` is.

One caveat the elevation pair does not share: a route's geocode lookup point is its
``start`` marker, which is coupled to the access radii — and those stay *tunable*
offline (only ``sample_interval_m`` is locked). So if the access radii change between
download and search, ``start`` can move off a recorded point and that route gracefully
falls back to its ``route/<id>`` label. With the radii unchanged (the common case) the
offline label equals the live one by construction.

Why a snapshot search is faithful: the download samples every geometry-plausible route
(``find_hikes`` with empty criteria), and the offline search re-derives the *same*
sample points — same saved ways -> same ``stitch_ways`` line -> same ``resample_by_distance``
at the **same** ``sample_interval_m`` (locked into the snapshot). The elevation values are
fixed, but ``gain_threshold``/``smooth_window`` are applied at search time, so those stay
retunable offline; only the sample interval is frozen.

Coordinates round-trip through JSON as lists; we restore them to tuples on load because
the geometry layer (``route_termini``, ``dict.fromkeys``) needs hashable points. Elevation
keys are rounded to ``_KEY_NDIGITS`` decimals (~1 cm) at both store and lookup so a hit
never depends on bit-exact float reproduction across two processes.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .elevation.base import Coord, ElevationError, ElevationProvider
from .geocode import Geocoder
from .overpass import AreaData
from .paths import user_cache_dir

# Bump when the on-disk shape changes incompatibly.
SNAPSHOT_VERSION = 1

# Elevation-key precision: 7 decimal degrees ≈ 1.1 cm — far finer than the ~25 m
# sample interval, so distinct samples never collide, while small float drift between
# the download process and a later search process can never miss a key.
_KEY_NDIGITS = 7


def _coord_key(pt: Coord) -> str:
    """Stable string key for an elevation sample point (rounded, comma-joined)."""
    return f"{round(pt[0], _KEY_NDIGITS)},{round(pt[1], _KEY_NDIGITS)}"


def default_snapshot_dir() -> Path:
    """Where named web-UI snapshots live: ``HIKE_SNAPSHOT_DIR`` or a per-user cache
    subdir (mirrors ``elevation.quota``'s state-dir convention)."""
    env = os.getenv("HIKE_SNAPSHOT_DIR")
    if env:
        return Path(env)
    return user_cache_dir() / "snapshots"


# --------------------------------------------------------------------------- providers


class RecordingElevationProvider(ElevationProvider):
    """Delegate to a real provider and remember every point it resolves.

    Used during a download: it returns exactly what the inner provider returns (so the
    download's geometry/filter behaviour is unchanged) while accumulating the
    ``point -> elevation`` map that becomes the snapshot. A failed batch raises through
    unchanged (``add_elevation`` then degrades that route to n/a) and simply records
    nothing for it — the offline search degrades the same route identically.
    """

    def __init__(self, inner: ElevationProvider):
        self.inner = inner
        self.samples: dict[Coord, float] = {}

    def lookup(self, points: list[Coord]) -> list[float]:
        elevations = self.inner.lookup(points)
        for pt, elev in zip(points, elevations):
            self.samples[pt] = elev
        return elevations


class SnapshotElevationProvider(ElevationProvider):
    """Answer elevation from a saved snapshot, never touching the network.

    Keys are matched at the snapshot's rounding precision. If *any* requested point is
    absent (e.g. a route whose download elevation failed), the whole batch raises
    ``ElevationError`` — the same all-or-nothing contract ``add_elevation`` already
    handles by leaving that route's gain/loss at ``None``.
    """

    def __init__(self, samples: dict[Coord, float]):
        # Re-key by the rounded string form so lookups match regardless of how the
        # caller's coordinates were produced.
        self._by_key: dict[str, float] = {_coord_key(pt): elev for pt, elev in samples.items()}

    def lookup(self, points: list[Coord]) -> list[float]:
        out: list[float] = []
        for pt in points:
            elev = self._by_key.get(_coord_key(pt))
            if elev is None:
                raise ElevationError("point not in snapshot (elevation unavailable offline)")
            out.append(elev)
        return out


class RecordingGeocoder(Geocoder):
    """Delegate to a real geocoder and remember every place it resolves.

    Used during a name-baking download: it returns exactly what the inner geocoder
    returns (so the download's naming behaviour is unchanged) while accumulating the
    ``point -> place`` map that becomes the snapshot's baked names. A point that resolves
    to nothing (the inner geocoder returns ``None``) is simply not recorded — the offline
    :class:`SnapshotGeocoder` returns ``None`` for any unrecorded point, so the route
    degrades to its ``route/<id>`` fallback identically whether the miss was a no-place
    result or an absent key.
    """

    def __init__(self, inner: Geocoder):
        self.inner = inner
        self.places: dict[Coord, str] = {}

    def reverse(self, point: Coord) -> str | None:
        name = self.inner.reverse(point)
        if name is not None:
            self.places[point] = name
        return name


class SnapshotGeocoder(Geocoder):
    """Answer reverse-geocoding from a saved snapshot, never touching the network.

    Mirrors :class:`SnapshotElevationProvider`: keys are matched at the snapshot's
    rounding precision, and an unrecorded point returns ``None`` — exactly the
    best-effort miss behaviour of the live geocoder, so the route keeps its
    ``route/<id>`` fallback. Because the *same* ``naming.enrich_names`` drives this as
    drives the live ``NominatimGeocoder``, an offline labelled search equals the live one
    by construction, modulo the access-radius caveat noted in this module's docstring.
    """

    def __init__(self, places: dict[Coord, str]):
        self._by_key: dict[str, str] = {_coord_key(pt): name for pt, name in places.items()}

    def reverse(self, point: Coord) -> str | None:
        return self._by_key.get(_coord_key(point))


# --------------------------------------------------------------------------- snapshot


@dataclass
class AreaSnapshot:
    """An area fetched once and searchable offline: geometry + elevation samples."""

    bbox: tuple[float, float, float, float]
    area: AreaData
    elevations: dict[Coord, float]
    sample_interval_m: float
    created_at: str = ""
    user_agent: str | None = None
    # Baked reverse-geocoded names for unnamed routes (``point -> place``), recorded at
    # download time when naming was opted into. Empty for snapshots downloaded without it
    # (and for pre-v2 snapshots) — those keep the honest offline no-op (see search.py).
    places: dict[Coord, str] = field(default_factory=dict)

    @property
    def route_count(self) -> int:
        return len(self.area.routes)

    @property
    def sample_count(self) -> int:
        return len(self.elevations)

    @property
    def place_count(self) -> int:
        return len(self.places)

    @property
    def poi_count(self) -> int:
        return len(self.area.pois)

    @property
    def ferrata_count(self) -> int | None:
        """Dedicated ferrata routes + cabled ways recorded, or ``None`` if the file
        predates the feature. ``None`` rather than 0 so a listing can say "not recorded"
        instead of implying an area was checked and found clear."""
        if self.area.ferrata_routes is None and self.area.ferrata_ways is None:
            return None
        return len(self.area.ferrata_routes or ()) + len(self.area.ferrata_ways or ())


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _route_to_json(r: dict) -> dict:
    """One route record. Shared by ``routes`` and ``ferrata_routes`` — they are the
    same shape by design (see overpass.AreaData), and two copies of this would be two
    chances for a cabled route to round-trip differently from a walkable one."""
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "ref": r.get("ref"),
        "osmc_color": r.get("osmc_color"),
        # Carry the source-of-truth "unnamed" flag (parse_area sets it). Without
        # it an offline search rebuilds every route as named=True, so enrich_names
        # would skip them and the baked place names would never apply — and
        # hike_to_dict would wrongly report unnamed=False for a route/<id> route.
        "unnamed": r.get("unnamed", False),
        "tags": r.get("tags", {}),
        "ways": [[[lat, lon] for lat, lon in way] for way in r["ways"]],
        # Parallel to "ways" (see overpass.parse_area). Absent in files written
        # before member-way tags were fetched; on load that absence keeps the
        # surface summary at None instead of "nothing is tagged".
        "way_tags": r.get("way_tags", []),
    }


def _area_to_json(area: AreaData) -> dict:
    out = {
        "routes": [_route_to_json(r) for r in area.routes],
        "parking": [
            {"coord": [p["coord"][0], p["coord"][1]], "name": p.get("name")}
            for p in area.parking
        ],
        "lifts": [
            {
                "stations": [[lat, lon] for lat, lon in lift["stations"]],
                "kind": lift.get("kind"),
                "name": lift.get("name"),
            }
            for lift in area.lifts
        ],
        # Points of interest (poi.py). Saved verbatim, NOT filtered to whatever the
        # download was interested in: the destination question is asked at *search*
        # time, so a snapshot must be able to answer any of them. Absent in files
        # written before this feature — see ``_area_from_json``.
        "pois": [
            {
                "coord": [p["coord"][0], p["coord"][1]],
                "kind": p.get("kind"),
                "name": p.get("name"),
            }
            for p in area.pois
        ],
        # Public-transport stops (access.TRANSIT_KINDS). The key is written even when
        # the area has NO stops — an empty list is the record of a real answer, and it
        # is what distinguishes this file from one written before transit existed. On
        # load, a MISSING key restores `None` ("never recorded") and the transit filter
        # then declines to answer rather than reporting every route as unreachable.
        "transit": [
            {
                "coord": [t["coord"][0], t["coord"][1]],
                "kind": t.get("kind"),
                "name": t.get("name"),
            }
            for t in (area.transit or [])
        ],
    }
    # WHICH kinds the "pois" list above was sorted into (overpass.AreaData.poi_kinds).
    # The key is written even when the area holds NO POIs — that pairing ("looked for all
    # of them, found none") is a real answer about the landscape, and is exactly what an
    # absent key cannot express.
    #
    # Unlike "transit" just above, the key is written CONDITIONALLY. Both fields are
    # tri-state, but transit can rely on the fact that a snapshot is only ever written
    # from a live parse (which always sets a list), whereas an AreaData with
    # `poi_kinds=None` is reachable here: load an old file and save it again. Writing
    # `[]` for it would upgrade "this file cannot say which kinds it covers" into "it
    # positively covered none" — a stronger claim than the file supports, which is the
    # one thing this whole field exists to stop.
    if area.poi_kinds is not None:
        out["poi_kinds"] = list(area.poi_kinds)
    # Cabled climbing (ferrata.py). Written CONDITIONALLY, for the same reason
    # `poi_kinds` is: an AreaData with `None` here is reachable (load an old file, save
    # it again), and writing `[]` for it would upgrade "this file never looked for
    # ferrata" into "it looked and found none" — the one claim a pre-feature file cannot
    # make, and precisely the one that would send somebody up a cable unwarned.
    #
    # Both keys are written together and only together, so a file can never say it knows
    # about ferrata routes but not ferrata ways. `--ferrata` reads both.
    if area.ferrata_routes is not None:
        out["ferrata_routes"] = [_route_to_json(r) for r in area.ferrata_routes]
    if area.ferrata_ways is not None:
        out["ferrata_ways"] = [
            {
                "id": w.get("id"),
                "coords": [[lat, lon] for lat, lon in w["coords"]],
                "name": w.get("name"),
                "scale": w.get("scale"),
            }
            for w in area.ferrata_ways
        ]
    return out


def _route_from_json(r: dict) -> dict:
    """Inverse of :func:`_route_to_json`, shared by both route lists."""
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "ref": r.get("ref"),
        "osmc_color": r.get("osmc_color"),
        # Default False so a pre-v2 snapshot (no "unnamed" key) loads unchanged.
        "unnamed": r.get("unnamed", False),
        "tags": r.get("tags", {}) or {},
        # Restore tuples — geometry de-dup/graph code needs hashable coords.
        "ways": [[(lat, lon) for lat, lon in way] for way in r["ways"]],
        "way_tags": r.get("way_tags", []),
    }


def _area_from_json(d: dict) -> AreaData:
    area = AreaData()
    for r in d.get("routes", []):
        area.routes.append(_route_from_json(r))
    for p in d.get("parking", []):
        c = p["coord"]
        area.parking.append({"coord": (c[0], c[1]), "name": p.get("name")})
    for lift in d.get("lifts", []):
        area.lifts.append(
            {
                "stations": [(lat, lon) for lat, lon in lift["stations"]],
                "kind": lift.get("kind"),
                "name": lift.get("name"),
            }
        )
    # ``.get`` with an empty default, exactly like ``places`` below: a snapshot written
    # before POIs existed simply loads with none, and ``search_snapshot`` warns loudly
    # rather than letting a POI filter return a silent empty result.
    for p in d.get("pois", []):
        c = p["coord"]
        area.pois.append(
            {"coord": (c[0], c[1]), "kind": p.get("kind"), "name": p.get("name")}
        )
    # Same presence detection as `transit` below, and for the same reason: a file without
    # the key was written by a build that did not record its registry, so it cannot say
    # which kinds it covers — distinct from a file that recorded a (possibly short) list.
    # `poi.unrecorded_kinds` turns the three states into the three things to tell a user.
    if "poi_kinds" in d:
        area.poi_kinds = tuple(str(k) for k in (d.get("poi_kinds") or ()))
    # Transit is the one field whose ABSENCE is meaningful, so it is read with a `None`
    # default rather than `[]`: a file without the key predates the feature and cannot
    # answer a transit question, while a file WITH an empty list positively recorded
    # "no stops in this area". Everything downstream keeps the two apart.
    if "transit" in d:
        area.transit = [
            {"coord": (t["coord"][0], t["coord"][1]), "kind": t.get("kind"),
             "name": t.get("name")}
            for t in (d.get("transit") or [])
        ]
    # Key presence again, and the stakes are higher here than anywhere else this pattern
    # is used: defaulting a pre-ferrata file to `[]` would let `--ferrata` answer "none
    # in this area" about a file that never asked the question.
    if "ferrata_routes" in d:
        area.ferrata_routes = [
            _route_from_json(r) for r in (d.get("ferrata_routes") or [])
        ]
    if "ferrata_ways" in d:
        area.ferrata_ways = [
            {
                "id": w.get("id"),
                "coords": [(lat, lon) for lat, lon in w["coords"]],
                "name": w.get("name"),
                "scale": w.get("scale"),
            }
            for w in (d.get("ferrata_ways") or [])
        ]
    return area


def snapshot_to_json(snap: AreaSnapshot) -> dict:
    """The serialisable form of a snapshot (used by ``save_snapshot`` and tests)."""
    return {
        "version": SNAPSHOT_VERSION,
        "created_at": snap.created_at or _now_iso(),
        "bbox": list(snap.bbox),
        "sample_interval_m": snap.sample_interval_m,
        "user_agent": snap.user_agent,
        "area": _area_to_json(snap.area),
        # Rounded string keys -> elevation. The dict round-trips exactly through JSON.
        "elevations": {_coord_key(pt): elev for pt, elev in snap.elevations.items()},
        # Baked reverse-geocoded place names, same rounded-key scheme. Optional: an empty
        # map (or a pre-v2 snapshot, where the key is absent) reads back as no baked names
        # — read via ``d.get("places", {})`` below so the version stays 1 (bumping it
        # would make ``snapshot_from_json`` reject every existing snapshot).
        "places": {_coord_key(pt): name for pt, name in snap.places.items()},
    }


def snapshot_from_json(d: dict) -> AreaSnapshot:
    if int(d.get("version", 0)) != SNAPSHOT_VERSION:
        raise ValueError(
            f"unsupported snapshot version {d.get('version')!r} "
            f"(this build reads v{SNAPSHOT_VERSION}) — re-download the area"
        )
    bbox = tuple(d["bbox"])  # type: ignore[assignment]
    elevations: dict[Coord, float] = {}
    for key, elev in d.get("elevations", {}).items():
        lat_s, lon_s = key.split(",")
        elevations[(float(lat_s), float(lon_s))] = float(elev)
    # Optional (added at v1, so pre-v2 files just lack the key): baked place names.
    places: dict[Coord, str] = {}
    for key, name in d.get("places", {}).items():
        lat_s, lon_s = key.split(",")
        places[(float(lat_s), float(lon_s))] = str(name)
    return AreaSnapshot(
        bbox=bbox,
        area=_area_from_json(d.get("area", {})),
        elevations=elevations,
        sample_interval_m=float(d["sample_interval_m"]),
        created_at=str(d.get("created_at", "")),
        user_agent=d.get("user_agent"),
        places=places,
    )


def save_snapshot(snap: AreaSnapshot, path: str | os.PathLike) -> None:
    """Write a snapshot to ``path`` as JSON, atomically (temp file + ``os.replace``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot_to_json(snap), ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_snapshot(path: str | os.PathLike) -> AreaSnapshot:
    """Read a snapshot JSON file back into an :class:`AreaSnapshot`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return snapshot_from_json(data)


def slug(name: str) -> str:
    """A safe snapshot filename stem: keep word chars and dashes, never a path."""
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in name).strip("_")


def snapshot_path(name: str) -> Path | None:
    """Where the *named* snapshot ``name`` lives, or ``None`` if the name is unusable."""
    stem = slug(name)
    if not stem:
        return None
    return default_snapshot_dir() / f"{stem}.json"


def list_snapshots() -> list[dict]:
    """Metadata for every NAMED snapshot on disk — "what have I already downloaded?".

    Reads the JSON header fields only (never the elevation map, which dominates the file
    size), so listing a dozen large areas stays instant. Each entry carries the bbox, so
    a frontend can draw the covered areas on a map instead of just naming them.

    Scope, stated because it is easy to misread: this enumerates the *named* snapshot
    directory (``HIKE_SNAPSHOT_DIR`` / the per-user cache subdir) — the namespace the
    web UI's "Download" writes to. A CLI ``--download some/path.json`` writes wherever
    you point it and is deliberately NOT tracked here; there is no registry of arbitrary
    paths, and inventing one would be a second source of truth. Unreadable or non-JSON
    files are skipped rather than failing the whole listing.
    """
    out: list[dict] = []
    d = default_snapshot_dir()
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            size = path.stat().st_size
        except (OSError, ValueError):
            continue
        area = data.get("area", {}) or {}
        out.append(
            {
                "name": path.stem,
                "path": str(path),
                "bbox": data.get("bbox"),
                "created_at": data.get("created_at"),
                "version": data.get("version"),
                "routes": len(area.get("routes", [])),
                "samples": len(data.get("elevations", {})),
                "places": len(data.get("places", {})),
                # Absent in pre-POI snapshots — 0 here is what the UI turns into
                # "re-download to search this area for churches/ruins".
                "pois": len(area.get("pois", [])),
                # The kind set this area was classified against, or `None` when the file
                # does not record one. Carried raw (not diffed against the registry) so
                # the inventory keeps reporting what the FILE says and one place —
                # `poi.unrecorded_kinds` — owns the comparison for every frontend.
                "poi_kinds": (
                    [str(k) for k in (area.get("poi_kinds") or [])]
                    if "poi_kinds" in area
                    else None
                ),
                "bytes": size,
            }
        )
    return out
