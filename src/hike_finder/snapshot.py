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


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _area_to_json(area: AreaData) -> dict:
    return {
        "routes": [
            {
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
            }
            for r in area.routes
        ],
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
    }


def _area_from_json(d: dict) -> AreaData:
    area = AreaData()
    for r in d.get("routes", []):
        area.routes.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "ref": r.get("ref"),
                "osmc_color": r.get("osmc_color"),
                # Default False so a pre-v2 snapshot (no "unnamed" key) loads unchanged.
                "unnamed": r.get("unnamed", False),
                "tags": r.get("tags", {}) or {},
                # Restore tuples — geometry de-dup/graph code needs hashable coords.
                "ways": [[(lat, lon) for lat, lon in way] for way in r["ways"]],
            }
        )
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
