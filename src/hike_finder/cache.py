"""Transparent on-disk cache for the two network seams: Overpass area fetches and
elevation-API lookups. SQLite-backed (stdlib ``sqlite3``), so it works across
processes (two ``hike-finder`` runs) and threads (the web server) without a server.

Why: OSM's usage policy asks clients to cache rather than re-fetch, and elevation
terrain never changes, so a point looked up once need never be requested again. The
cache is *transparent* — a cached run returns exactly what an uncached run would; it
only removes redundant network calls. It is NOT the snapshot feature (``snapshot.py``):
snapshots are explicit, named, portable, offline-forever files you manage; this cache
is invisible plumbing that quietly spares the public servers on repeat/overlapping
live searches.

The stores, and their staleness models:
  - **elevation** — keyed by ``(endpoint, rounded coord)``. Terrain is immutable, so
    there is **no TTL**: a point is cached forever. Keyed by the FULL endpoint, not
    just the host, because OpenTopoData ``srtm30m`` and ``aster30m`` share a host but
    return different elevations — host-keying would cross-serve. Because OSM route
    relations carry full member geometry regardless of the query bbox, the *same*
    route resamples to the *same* points across different overlapping bboxes, so this
    cache hits across bbox changes, not only exact re-runs.
  - **overpass** — keyed by ``sha256(url + query)`` (the query already encodes the
    bbox; the hash auto-invalidates if ``build_query`` changes shape). Trails DO
    change, slowly, so a **TTL** applies (default via config, 0 disables).
  - **geocode / place_search** — the two directions of Nominatim, kept apart because
    they answer different questions: a coordinate's place NAME (keyed by rounded coord,
    one string) and a typed name's candidate PLACES (keyed by the query text, a list
    with extents). Both TTL'd, both negative-cached, so a misspelling and an empty
    hillside each cost one request rather than one per run.

Robustness: every DB operation degrades to a clean miss / no-op on ANY sqlite or
filesystem error (corrupt db, locked, disk full, read-only). A broken cache must be
invisible, never fatal — this mirrors ``elevation/quota.py``'s defensive reads. The
elevation table grows unbounded, but at ~tens of bytes per point that is tens of MB
even for millions of points; no eviction for a personal tool.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .elevation.base import ElevationProvider
from .geocode import Geocoder, PlaceMatch, PlaceSearcher
from .paths import user_cache_dir
from .snapshot import SNAPSHOT_VERSION, _area_from_json, _area_to_json, _coord_key

# Bump if the table layout changes incompatibly (folded into the overpass key so a
# stale-shaped cached area is never read back).
_SCHEMA_VERSION = 1
# Keep an ``IN (...)`` parameter list under SQLite's default 999-variable limit; a
# route can resample to thousands of points, well over one statement's worth.
_SELECT_CHUNK = 400
_BUSY_TIMEOUT_MS = 5000


def _default_cache_dir() -> Path:
    """Per-user cache dir, same convention as ``elevation.quota`` / ``snapshot``."""
    return user_cache_dir()


def cache_path_from_config(cfg) -> Path:
    # Belt-and-suspenders: ``Config.cache_dir`` already reads ``HIKE_CACHE_DIR``
    # live at ``load()`` time, but resolve it here too (mirrors
    # ``elevation.quota._default_state_dir``) so a caller passing a bare/legacy cfg
    # object — or a test poking the env — can still redirect the cache.
    d = getattr(cfg, "cache_dir", None) or os.getenv("HIKE_CACHE_DIR")
    base = Path(d) if d else _default_cache_dir()
    return base / "cache.sqlite3"


def from_config(cfg) -> Cache | None:
    """A :class:`Cache` for this config, or ``None`` when caching is disabled
    (``HIKE_CACHE=0`` / ``--no-cache``). ``None`` makes every call site fall straight
    through to the network — the cache is opt-out, on by default."""
    if not getattr(cfg, "cache_enabled", True):
        return None
    try:
        return Cache(cache_path_from_config(cfg))
    except (sqlite3.Error, OSError):
        return None


def area_cache_key(overpass_url: str, query: str) -> str:
    """Stable cache key for one Overpass fetch: a hash of the endpoint + the exact
    query (which encodes the bbox), namespaced by the on-disk format versions."""
    raw = f"v{_SCHEMA_VERSION}/snap{SNAPSHOT_VERSION}\n{overpass_url}\n{query}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(fetched_at: str, now: datetime | None = None) -> float | None:
    """Age of an ISO timestamp in seconds, or ``None`` if it can't be parsed."""
    try:
        ts = datetime.fromisoformat(fetched_at)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ((now or _utcnow()) - ts).total_seconds()


class Cache:
    """SQLite-backed store for elevation points and Overpass areas.

    Every method is failure-isolated: any sqlite/OS error degrades to a miss
    (reads) or a silent no-op (writes), so a corrupt or locked cache can never break
    a search. ``self._ok`` is ``False`` if the DB couldn't even be created, short-
    circuiting all operations.
    """

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._ok = self._init_db()

    # -- connection / schema ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=_BUSY_TIMEOUT_MS / 1000)
        con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        con.execute("PRAGMA journal_mode=WAL")  # concurrent readers + one writer
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    def _init_db(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as con:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS elevation ("
                    "source TEXT NOT NULL, coord TEXT NOT NULL, elevation REAL NOT NULL, "
                    "PRIMARY KEY (source, coord))"
                )
                con.execute(
                    "CREATE TABLE IF NOT EXISTS overpass ("
                    "key TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                # Reverse-geocoded place names (see geocode.py). Keyed by
                # (endpoint, rounded coord) like elevation, but place names DO change
                # (slowly), so a TTL applies — and a resolved-to-nothing point is
                # stored as "" (a NEGATIVE cache) so we don't re-hit Nominatim for it.
                con.execute(
                    "CREATE TABLE IF NOT EXISTS geocode ("
                    "source TEXT NOT NULL, coord TEXT NOT NULL, place TEXT NOT NULL, "
                    "fetched_at TEXT NOT NULL, PRIMARY KEY (source, coord))"
                )
                # FORWARD lookups (a typed name -> the places it could mean). A separate
                # table rather than a row in ``geocode``: that one is keyed by coordinate
                # and holds a single name, and a forward answer is a LIST of candidates
                # with extents — there is no honest way to fold one into the other. Same
                # TTL model as ``geocode`` (names change slowly) and the same negative
                # caching: "[]" records that Nominatim positively found nothing.
                con.execute(
                    "CREATE TABLE IF NOT EXISTS place_search ("
                    "source TEXT NOT NULL, query TEXT NOT NULL, payload TEXT NOT NULL, "
                    "fetched_at TEXT NOT NULL, PRIMARY KEY (source, query))"
                )
            return True
        except (sqlite3.Error, OSError):
            return False

    # -- elevation ----------------------------------------------------------

    def get_elevations(self, source: str, coords: list) -> dict:
        """``{coord: elevation}`` for the subset of ``coords`` already cached.

        Keys are the caller's own coord objects (not the rounded form), so the
        caller can look results up by identity. A missing point is simply absent.
        """
        if not self._ok or not coords:
            return {}
        # Map rounded key -> the caller's coord, so we return their objects back.
        key_to_coord: dict[str, tuple] = {}
        for c in coords:
            key_to_coord.setdefault(_coord_key(c), c)
        hits: dict = {}
        try:
            with self._connect() as con:
                keys = list(key_to_coord)
                for i in range(0, len(keys), _SELECT_CHUNK):
                    chunk = keys[i : i + _SELECT_CHUNK]
                    placeholders = ",".join("?" * len(chunk))
                    rows = con.execute(
                        "SELECT coord, elevation FROM elevation "
                        f"WHERE source=? AND coord IN ({placeholders})",
                        [source, *chunk],
                    ).fetchall()
                    for key, elev in rows:
                        hits[key_to_coord[key]] = float(elev)
        except (sqlite3.Error, OSError):
            return {}
        return hits

    def put_elevations(self, source: str, mapping: dict) -> None:
        """Store ``{coord: elevation}``. Existing rows are kept (INSERT OR IGNORE) —
        values are deterministic, so this is idempotent and concurrency-safe."""
        if not self._ok or not mapping:
            return
        rows = [(source, _coord_key(c), float(e)) for c, e in mapping.items()]
        try:
            with self._connect() as con:
                con.executemany(
                    "INSERT OR IGNORE INTO elevation (source, coord, elevation) VALUES (?, ?, ?)",
                    rows,
                )
        except (sqlite3.Error, OSError):
            pass

    # -- overpass -----------------------------------------------------------

    def get_area(self, key: str, ttl_seconds: float | None, now: datetime | None = None):
        """Cached :class:`~hike_finder.overpass.AreaData` for ``key``, or ``None`` on
        miss / expiry. ``ttl_seconds=None`` disables expiry; a negative or unparseable
        stored timestamp is treated as expired."""
        if not self._ok:
            return None
        try:
            with self._connect() as con:
                row = con.execute(
                    "SELECT fetched_at, payload FROM overpass WHERE key=?", (key,)
                ).fetchone()
        except (sqlite3.Error, OSError):
            return None
        if not row:
            return None
        fetched_at, payload = row
        if ttl_seconds is not None:
            age = _age_seconds(fetched_at, now)
            if age is None or age > ttl_seconds:
                return None
        try:
            return _area_from_json(json.loads(payload))
        except (ValueError, KeyError, TypeError):
            return None

    def put_area(self, key: str, area, now: datetime | None = None) -> None:
        if not self._ok:
            return
        try:
            payload = json.dumps(_area_to_json(area), ensure_ascii=False)
            stamp = (now or _utcnow()).replace(microsecond=0).isoformat()
            with self._connect() as con:
                con.execute(
                    "INSERT OR REPLACE INTO overpass (key, fetched_at, payload) VALUES (?, ?, ?)",
                    (key, stamp, payload),
                )
        except (sqlite3.Error, OSError, ValueError, TypeError):
            pass

    # -- geocode ------------------------------------------------------------

    def get_place(self, source: str, coord, ttl_seconds: float | None, now=None):
        """Cached place name for ``coord`` under ``source`` (the geocoder endpoint).

        Returns the stored string — which may be ``""`` for a *negative* hit (the point
        resolved to no place) — or ``None`` on a miss / expiry. A caller distinguishes
        "cached as no-name" (``""``) from "never looked up" (``None``) this way, so a
        point with no settlement isn't re-queried every run.
        """
        if not self._ok:
            return None
        try:
            with self._connect() as con:
                row = con.execute(
                    "SELECT place, fetched_at FROM geocode WHERE source=? AND coord=?",
                    (source, _coord_key(coord)),
                ).fetchone()
        except (sqlite3.Error, OSError):
            return None
        if not row:
            return None
        place, fetched_at = row
        if ttl_seconds is not None:
            age = _age_seconds(fetched_at, now)
            if age is None or age > ttl_seconds:
                return None
        return place

    def put_place(self, source: str, coord, place: str, now=None) -> None:
        """Store ``coord -> place`` (``place=""`` records a negative result)."""
        if not self._ok:
            return
        try:
            stamp = (now or _utcnow()).replace(microsecond=0).isoformat()
            with self._connect() as con:
                con.execute(
                    "INSERT OR REPLACE INTO geocode (source, coord, place, fetched_at) "
                    "VALUES (?, ?, ?, ?)",
                    (source, _coord_key(coord), place, stamp),
                )
        except (sqlite3.Error, OSError):
            pass

    # -- forward place search -----------------------------------------------

    def get_place_search(self, source: str, query: str, ttl_seconds: float | None, now=None):
        """Cached matches for a typed ``query``, or ``None`` on a miss / expiry.

        A cached *empty list* is a real answer ("Nominatim knows no such place") and is
        returned as ``[]``, distinct from the ``None`` of "never asked" — the same
        negative-caching split ``get_place`` makes, and for the same reason: a
        misspelling should not re-hit a rate-limited public server on every run.
        """
        if not self._ok:
            return None
        try:
            with self._connect() as con:
                row = con.execute(
                    "SELECT payload, fetched_at FROM place_search WHERE source=? AND query=?",
                    (source, query),
                ).fetchone()
        except (sqlite3.Error, OSError):
            return None
        if not row:
            return None
        payload, fetched_at = row
        if ttl_seconds is not None:
            age = _age_seconds(fetched_at, now)
            if age is None or age > ttl_seconds:
                return None
        try:
            raw = json.loads(payload)
        except (ValueError, TypeError):
            return None
        if not isinstance(raw, list):
            return None
        return [_match_from_json(m) for m in raw if isinstance(m, dict)]

    def put_place_search(self, source: str, query: str, matches, now=None) -> None:
        """Store ``query -> matches`` (an empty list records a negative result)."""
        if not self._ok:
            return
        try:
            payload = json.dumps([_match_to_json(m) for m in matches], ensure_ascii=False)
            stamp = (now or _utcnow()).replace(microsecond=0).isoformat()
            with self._connect() as con:
                con.execute(
                    "INSERT OR REPLACE INTO place_search (source, query, payload, fetched_at) "
                    "VALUES (?, ?, ?, ?)",
                    (source, query, payload, stamp),
                )
        except (sqlite3.Error, OSError, ValueError, TypeError):
            pass

    def clear(self) -> None:
        """Empty every store (used by ``hike-finder --clear-cache`` and tests)."""
        if not self._ok:
            return
        try:
            with self._connect() as con:
                con.execute("DELETE FROM elevation")
                con.execute("DELETE FROM overpass")
                con.execute("DELETE FROM geocode")
                con.execute("DELETE FROM place_search")
        except (sqlite3.Error, OSError):
            pass


class CachingElevationProvider(ElevationProvider):
    """Wrap an elevation provider with a persistent point cache.

    On ``lookup``: serve known points from the cache, ask the inner provider for the
    misses only (one batched call), store the new results, then reassemble in the
    caller's order. The contract is unchanged — order and length are preserved, and
    if the inner provider raises ``ElevationError`` on the misses it propagates with
    nothing stored, so the route degrades to n/a exactly as it would uncached.
    """

    def __init__(self, cache: Cache, source: str, inner: ElevationProvider):
        self.cache = cache
        self.source = source
        self.inner = inner

    def lookup(self, points: list) -> list:
        if not points:
            return []
        cached = self.cache.get_elevations(self.source, points)
        # Dedupe misses preserving order — a route can revisit the same point.
        misses = list(dict.fromkeys(p for p in points if p not in cached))
        if misses:
            fetched = self.inner.lookup(misses)  # raises -> propagate, store nothing
            # strict: the provider contract is one elevation per point. A short reply
            # would otherwise be cached as a wrong point-to-elevation pairing.
            new = dict(zip(misses, fetched, strict=True))
            self.cache.put_elevations(self.source, new)
            cached = {**cached, **new}
        return [cached[p] for p in points]


def _match_to_json(m: PlaceMatch) -> dict:
    return {
        "name": m.name,
        "lat": m.point[0],
        "lon": m.point[1],
        "bbox": list(m.bbox) if m.bbox else None,
        "country": m.country,
        "kind": m.kind,
        "osm_type": m.osm_type,
        "osm_id": m.osm_id,
    }


def _match_from_json(d: dict) -> PlaceMatch:
    raw = d.get("bbox")
    bbox = (
        (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        if isinstance(raw, list) and len(raw) == 4
        else None
    )
    return PlaceMatch(
        name=str(d.get("name") or ""),
        point=(float(d.get("lat", 0.0)), float(d.get("lon", 0.0))),
        bbox=bbox,
        country=d.get("country"),
        kind=d.get("kind"),
        osm_type=d.get("osm_type"),
        osm_id=d.get("osm_id"),
    )


class CachingPlaceSearch(PlaceSearcher):
    """Wrap a :class:`~hike_finder.geocode.PlaceSearcher` with a persistent cache.

    The key folds in the requested ``limit``: a run that asked for 3 candidates and one
    that asked for 8 are different questions, and serving the short answer to the long
    question would hide alternatives the user is being invited to choose between.

    A FAILURE is never cached. The inner searcher raises rather than returning ``None``
    (see ``geocode``'s module docstring), and that exception propagates untouched — so a
    Nominatim outage costs the user an error they can act on, not a stored "no such
    place" that outlives the outage by the length of the TTL.
    """

    def __init__(self, cache: Cache, source: str, inner: PlaceSearcher, ttl_seconds: float):
        self.cache = cache
        self.source = source
        self.inner = inner
        self.ttl_seconds = ttl_seconds

    def search(self, query: str, *, limit: int = 5) -> list[PlaceMatch]:
        key = f"{int(limit)}|{' '.join((query or '').split()).casefold()}"
        cached = self.cache.get_place_search(self.source, key, self.ttl_seconds)
        if cached is not None:  # hit (possibly [] for a cached negative)
            return cached
        matches = self.inner.search(query, limit=limit)
        self.cache.put_place_search(self.source, key, matches)
        return matches


class CachingGeocoder(Geocoder):
    """Wrap a :class:`~hike_finder.geocode.Geocoder` with a persistent place cache.

    A coordinate is reverse-geocoded at most once per TTL window — across runs and
    across routes that share a trailhead — which is what keeps the opt-in naming
    feature a polite Nominatim citizen. A negative result ("" — no place mapped) is
    cached too, so an empty point isn't re-queried. The inner geocoder is best-effort
    (returns ``None`` on failure), so this never raises either.
    """

    def __init__(self, cache: Cache, source: str, inner: Geocoder, ttl_seconds: float):
        self.cache = cache
        self.source = source
        self.inner = inner
        self.ttl_seconds = ttl_seconds

    def reverse(self, point):
        cached = self.cache.get_place(self.source, point, self.ttl_seconds)
        if cached is not None:  # hit (possibly "" for a cached negative)
            return cached or None
        name = self.inner.reverse(point)
        self.cache.put_place(self.source, point, name or "")
        return name
