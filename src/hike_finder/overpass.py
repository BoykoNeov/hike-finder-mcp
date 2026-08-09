"""Fetch hiking routes — plus parking and chairlift features — from OSM.

We target route RELATIONS (route=hiking/foot), not raw highway=path ways.
Relations are the signed, named, maintained trails — including the Czech KČT
network that mapy.cz renders — which is what gives results the "mapy.cz feel"
instead of every unmarked path.

In ONE Overpass round-trip we also pull the features the other filters need:
  - amenity=parking  (car access; ``out center`` gives a representative coord)
  - ride-up aerialways (chairlift access; ``out geom`` gives station endpoints)
  - public-transport stops (access.TRANSIT_KINDS — stations, halts, tram/bus stops;
    ``out center``)
  - the member ways' OWN tags (``way(r); out tags;``) for surface/tracktype. A route
    relation carries neither — they live on the ways, and ``out body geom`` returns
    member geometry WITHOUT member tags, so this second statement is the only way to
    see them. It costs about +22 % response size (measured: 712 KB -> 866 KB on a
    Krkonoše box) and returns no geometry, only tags to join back by way id.
  - every registered point of interest (poi.POI_KINDS — churches, ruins, peaks…;
    ``out center`` again). These come along on EVERY query, not only when a POI
    filter is set: one query shape means one Overpass cache key and a snapshot that
    always carries its POIs, so an offline POI search works by construction.

Returns lightweight dicts; geometry assembly/distance/elevation/access happen
downstream so this module stays a thin transport layer.

The HTTP call can't run in the build sandbox (network restricted), so
``fetch_area`` is validated live on your machine — but the risky bit, parsing
the mixed-element response, is split into the PURE ``parse_area`` and is
unit-tested offline against a hand-built sample. Respect Overpass usage policy:
cache results, throttle, and prefer a self-hosted/regional instance for heavy use.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version

import requests

from .access import RIDE_UP_AERIALWAYS, classify_transit, transit_selectors_by_key
from . import poi as _poi

Coord = tuple[float, float]

DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# overpass-api.de sits behind Apache/mod_security, which rejects the default
# python-requests User-Agent with "406 Not Acceptable" before the query is even
# parsed. A descriptive UA is REQUIRED, not optional — confirmed live. Set a real
# contact via HIKE_OVERPASS_UA (wired through config.py -> server.py) per OSM
# etiquette; this default works but names no contact.
try:
    _VERSION = version("hike-finder-mcp")
except PackageNotFoundError:  # raw checkout, not pip-installed
    _VERSION = "0"
USER_AGENT = (
    f"hike-finder-mcp/{_VERSION} "
    "(OSM hiking route search; set HIKE_OVERPASS_UA with your contact)"
)

# The public instance frequently answers small queries with a transient 504/429
# under load. A short bounded backoff makes the tool usable without hammering.
_TRANSIENT_STATUS = {429, 502, 503, 504}


@dataclass
class AreaData:
    """Everything fetched for one bounding box."""

    routes: list[dict] = field(default_factory=list)
    parking: list[dict] = field(default_factory=list)  # {"coord", "name"}
    lifts: list[dict] = field(default_factory=list)  # {"stations", "kind", "name"}
    # Public-transport stops (see access.TRANSIT_KINDS): {"coord", "kind", "name"}.
    # `None` — not `[]` — is the default ON PURPOSE, and only here: an AreaData built
    # before this feature (a v1 snapshot on disk) genuinely does not KNOW whether the
    # area has transit, which is a different claim from "it has none". The tri-state
    # survives all the way to the filter, which refuses to answer a transit question
    # from a snapshot that never recorded one. A live fetch always sets a list.
    transit: list[dict] | None = None
    # Registered points of interest (see poi.py): {"coord", "kind", "name"}. Default
    # empty so every AreaData built before this feature — including a pre-existing
    # snapshot — loads unchanged and simply matches no POI filter.
    pois: list[dict] = field(default_factory=list)


def _poi_clauses(bbox: str) -> str:
    """One ``nwr[key~"^(v1|v2|…)$"]`` clause per POI tag key, from the registry.

    Derived from ``poi.selectors_by_key()`` rather than written out here, so the query
    and the classifier can never disagree about which objects exist (see poi.py). One
    regex per key keeps the query compact as the registry grows.
    """
    lines = []
    for key, values in _poi.selectors_by_key().items():
        alt = "|".join(values)
        lines.append(f'    nwr["{key}"~"^({alt})$"]({bbox});\n    out center;')
    return "\n".join(lines)


def _transit_clauses(bbox: str) -> str:
    """One clause per transit tag key, from ``access.TRANSIT_KINDS``.

    Same one-registry-two-derivations rule as ``_poi_clauses``: the query and
    ``access.classify_transit`` read the same table, so they cannot disagree about
    which stops exist.
    """
    lines = []
    for key, values in transit_selectors_by_key().items():
        alt = "|".join(values)
        lines.append(f'    nwr["{key}"~"^({alt})$"]({bbox});\n    out center;')
    return "\n".join(lines)


def build_query(
    south: float, west: float, north: float, east: float, timeout_s: int = 60
) -> str:
    """Overpass QL: hiking routes + parking + aerialways + transit stops + POIs."""
    bbox = f"{south},{west},{north},{east}"
    lift_re = "|".join(sorted(RIDE_UP_AERIALWAYS))
    return f"""
    [out:json][timeout:{timeout_s}];
    (
      relation["route"="hiking"]({bbox});
      relation["route"="foot"]({bbox});
    );
    out body geom;
    way(r);
    out tags;
    nwr["amenity"="parking"]({bbox});
    out center;
    way["aerialway"~"^({lift_re})$"]({bbox});
    out geom;
{_transit_clauses(bbox)}
{_poi_clauses(bbox)}
    """


def _representative_coord(el: dict) -> Coord | None:
    """A single (lat, lon) for a parking element (node, or way/area via center)."""
    if "lat" in el and "lon" in el:  # node
        return (el["lat"], el["lon"])
    center = el.get("center")  # way / relation with `out center`
    if center:
        return (center["lat"], center["lon"])
    geom = el.get("geometry")  # fallback: first vertex
    if geom:
        return (geom[0]["lat"], geom[0]["lon"])
    return None


def _way_endpoints(el: dict) -> list[Coord]:
    """Both stations of an aerialway way (where you board), de-duped if a ring."""
    geom = el.get("geometry")
    if not geom:
        return []
    head = (geom[0]["lat"], geom[0]["lon"])
    tail = (geom[-1]["lat"], geom[-1]["lon"])
    return [head] if head == tail else [head, tail]


def _is_tag_only_way(el: dict) -> bool:
    """True for a record produced by ``way(r); out tags;`` — tags and an id, but no
    geometry of ANY kind (no ``geometry``, no ``center``, no ``lat``). That absence is
    what distinguishes it from a parking way (``out center``) or an aerialway
    (``out geom``), both of which are matched by the feature branches."""
    return (
        el.get("type") == "way"
        and "geometry" not in el
        and "center" not in el
        and "lat" not in el
    )


def parse_area(elements: list[dict]) -> AreaData:
    """Pure: split a mixed Overpass element list into routes/parking/lifts.

    Branching is by tag, not element type, so a parking *way* and an aerialway
    *way* never collide. This is the failure-prone part of the network layer,
    so it lives here, isolated and unit-tested without a live endpoint.
    """
    # A live parse always KNOWS the transit answer, even when it is "none here" — so
    # the list starts empty rather than at the not-recorded `None` default.
    area = AreaData(transit=[])
    # FIRST: the tag-only member-way records from `way(r); out tags;`, keyed by way id
    # so the relation branch below can join them onto its members. They must also be
    # skipped by the feature branches — a member way carrying, say, `tourism=viewpoint`
    # would otherwise be filed as a POI at no coordinate. (The branches happen to be
    # coord-guarded already, so nothing leaks today; this makes it deliberate.)
    way_tags: dict[object, dict] = {
        el["id"]: (el.get("tags") or {})
        for el in elements
        if _is_tag_only_way(el) and el.get("id") is not None
    }
    # A POI can satisfy two registry clauses at once (a shelter that is also a picnic
    # site), and Overpass emits an element once per matching statement — so key the POI
    # list by the element's identity to keep one entry per real-world object.
    seen_pois: set[tuple[str, object]] = set()
    seen_transit: set[tuple[str, object]] = set()
    for el in elements:
        if _is_tag_only_way(el):
            continue  # already harvested into way_tags above
        tags = el.get("tags", {}) or {}
        if el.get("type") == "relation" and tags.get("route") in ("hiking", "foot"):
            ways: list[list[Coord]] = []
            # Kept strictly parallel to `ways` — index i of one describes index i of
            # the other — because surface is measured per member and weighted by that
            # member's length. A dict keyed by way id would lose the pairing whenever
            # a relation includes the same way twice (an out-and-back leg does).
            member_tags: list[dict] = []
            for member in el.get("members", []):
                if member.get("type") == "way" and "geometry" in member:
                    ways.append([(pt["lat"], pt["lon"]) for pt in member["geometry"]])
                    member_tags.append(way_tags.get(member.get("ref"), {}))
            if not ways:
                continue
            area.routes.append(
                {
                    "id": el.get("id"),
                    "name": tags.get("name") or tags.get("ref") or f"route/{el.get('id')}",
                    "ref": tags.get("ref"),
                    # An explicit "this route has no signed name" signal, carried from
                    # the source of truth (the tags, here) rather than reconstructed
                    # downstream from the `route/<id>` fallback string. Drives optional
                    # reverse-geocode naming (see naming.py / search.name_places).
                    "unnamed": not (tags.get("name") or tags.get("ref")),
                    "osmc_color": tags.get("osmc:symbol"),  # KČT marking, if present
                    "tags": tags,
                    "ways": ways,
                    # Parallel to "ways". An empty dict means "this way carried no
                    # tags we asked for"; the LIST being empty overall means the data
                    # predates the member-tag fetch, and the surface summary is then
                    # simply absent rather than reported as untagged ground.
                    "way_tags": member_tags,
                }
            )
        elif tags.get("amenity") == "parking":
            coord = _representative_coord(el)
            if coord:
                area.parking.append({"coord": coord, "name": tags.get("name")})
        elif tags.get("aerialway") in RIDE_UP_AERIALWAYS:
            stations = _way_endpoints(el)
            if stations:
                area.lifts.append(
                    {
                        "stations": stations,
                        "kind": tags.get("aerialway"),
                        "name": tags.get("name"),
                    }
                )
        elif classify_transit(tags) is not None:
            # Above the POI branch, with parking and lifts, for the same reason: an
            # access feature that is ALSO tagged like a destination is access first.
            # (No registry kind uses the `railway`/`highway` keys, so today nothing
            # actually contends — the ordering is here so it stays true if one does.)
            kind = classify_transit(tags)
            ident = (el.get("type", ""), el.get("id"))
            coord = _representative_coord(el)
            if coord and ident not in seen_transit:
                seen_transit.add(ident)
                area.transit.append(
                    {"coord": coord, "kind": kind, "name": tags.get("name")}
                )
        else:
            # Points of interest, LAST so the access branches above always win a
            # double-tagged element (a parking lot is car access, not a destination).
            kind = _poi.classify(tags)
            if kind is None:
                continue
            ident = (el.get("type", ""), el.get("id"))
            if ident in seen_pois:
                continue
            coord = _representative_coord(el)
            if coord:
                seen_pois.add(ident)
                area.pois.append(
                    {"coord": coord, "kind": kind, "name": tags.get("name")}
                )
    return area


def fetch_area(
    south: float,
    west: float,
    north: float,
    east: float,
    overpass_url: str = DEFAULT_OVERPASS_URL,
    timeout_s: float = 90.0,
    user_agent: str | None = None,
    max_retries: int = 3,
) -> AreaData:
    """Fetch routes + parking + lift features for a bounding box (one request)."""
    query = build_query(south, west, north, east)
    headers = {"User-Agent": user_agent or USER_AGENT}

    resp = None
    for attempt in range(max_retries):
        resp = requests.post(
            overpass_url, data={"data": query}, headers=headers, timeout=timeout_s
        )
        if resp.status_code not in _TRANSIENT_STATUS:
            break
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)  # 1s, 2s, ... brief backoff on overload
    if resp is None:  # max_retries < 1 sent nothing — fail cleanly, not AttributeError
        raise ValueError("max_retries must be >= 1")
    resp.raise_for_status()

    elements = resp.json().get("elements", [])
    return parse_area(elements)
