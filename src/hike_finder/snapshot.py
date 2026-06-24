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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .elevation.base import Coord, ElevationError, ElevationProvider
from .overpass import AreaData

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
    base = (
        os.getenv("LOCALAPPDATA")
        or os.getenv("XDG_CACHE_HOME")
        or os.path.join(Path.home(), ".cache")
    )
    return Path(base) / "hike-finder" / "snapshots"


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

    @property
    def route_count(self) -> int:
        return len(self.area.routes)

    @property
    def sample_count(self) -> int:
        return len(self.elevations)


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
    return AreaSnapshot(
        bbox=bbox,
        area=_area_from_json(d.get("area", {})),
        elevations=elevations,
        sample_interval_m=float(d["sample_interval_m"]),
        created_at=str(d.get("created_at", "")),
        user_agent=d.get("user_agent"),
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
