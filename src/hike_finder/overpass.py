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
from . import ferrata as _ferrata
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
    # WHICH kinds the above was classified against (`poi.all_kinds()` at fetch time), so
    # a saved area searched by a later build can distinguish "no named trees here" from
    # "nobody looked for trees". `None` — like `transit`, and for the same reason — means
    # the area does not record it, which is a third answer and not a synonym for either.
    # An empty `pois` list under a FULL kind set is a real answer about the landscape;
    # that is the whole distinction this field buys. A live fetch always sets it.
    poi_kinds: tuple[str, ...] | None = None
    # Cabled climbing (see ferrata.py). BOTH default to `None`, the `transit` tri-state:
    # an area built before this feature does not know whether ferrata are here, which is
    # not the same claim as "there are none".
    #
    # `ferrata_routes` holds `route=via_ferrata` relations in the SAME dict shape as
    # `routes`, so every downstream measurement works on them unchanged — but in a list
    # of their own, deliberately. Dropping them into `routes` behind a boolean would put
    # a cabled climb one forgotten `if` away from an ordinary result list and, worse,
    # from the graph `--compose-loops` stitches synthetic loops out of. A separate list
    # makes that leak impossible by construction rather than by vigilance in six places.
    ferrata_routes: list[dict] | None = None
    # Individual cabled ways in the box: {"id", "coords", "name", "scale"}. Most are
    # already member ways of a hiking relation (measured: 40 of 46 in Cortina, 26 of 30
    # in Ehrwald), so this is NOT where avoidance gets its answer — that rides on
    # `way_tags`, which covers every way a returned route can be built from. This list
    # exists so the minority that belong to no relation are still browsable.
    ferrata_ways: list[dict] | None = None


def _tag_filter(key: str, values: tuple[str, ...]) -> str:
    """``["key"]`` (presence) or ``["key"~"^(a|b)$"]`` — one bracket filter.

    Empty ``values`` renders the bare presence test, which is what
    ``poi.PoiKind.require`` means by an empty value tuple and what ``poi._has_tag``
    checks with ``key in tags``. The two readings have to match or the query fetches
    objects the classifier drops.
    """
    return f'["{key}"]' if not values else f'["{key}"~"^({"|".join(values)})$"]'


def _poi_clauses(bbox: str) -> str:
    """One ``nwr[key~"^(v1|v2|…)$"]`` clause per POI tag key, from the registry.

    Derived from ``poi.selectors_by_key()`` rather than written out here, so the query
    and the classifier can never disagree about which objects exist (see poi.py). One
    regex per key keeps the query compact as the registry grows.

    Kinds carrying a ``require`` get their OWN clause after the merged ones, with the
    required tags appended as extra bracket filters. That is the one place a POI clause
    is narrower than "the whole key", and it is narrower on purpose: ``natural=tree`` is
    thousands of street trees in a hiking box, of which the ~1 % that are NAMED are the
    walk destination. A deny-list stays out of the query (it does not change the cache
    key); a requirement cannot, because the point of it is to not fetch the rest.
    """
    lines = []
    for key, values in _poi.selectors_by_key().items():
        alt = "|".join(values)
        lines.append(f'    nwr["{key}"~"^({alt})$"]({bbox});\n    out center;')
    for key, values, require in _poi.required_selectors():
        alt = "|".join(values)
        extra = "".join(_tag_filter(k, vals) for k, vals in require)
        lines.append(f'    nwr["{key}"~"^({alt})$"]{extra}({bbox});\n    out center;')
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
    """Overpass QL: hiking routes + parking + aerialways + transit stops + POIs + ferrata.

    ``route=via_ferrata`` rides in the SAME union as the hiking relations, rather than
    in a clause of its own, so the existing ``way(r); out tags;`` picks up its member
    ways for free — one statement, no second join. It is split back out by tag in
    ``parse_area``, into a list of its own that ordinary searches never see.
    """
    bbox = f"{south},{west},{north},{east}"
    lift_re = "|".join(sorted(RIDE_UP_AERIALWAYS))
    return f"""
    [out:json][timeout:{timeout_s}];
    (
      relation["route"="hiking"]({bbox});
      relation["route"="foot"]({bbox});
      relation["route"="via_ferrata"]({bbox});
    );
    out body geom;
    way(r);
    out tags;
    (
      way["highway"="{_ferrata.FERRATA_HIGHWAY}"]({bbox});
      way["{_ferrata.SCALE_KEY}"]({bbox});
    );
    out geom;
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
    # the list starts empty rather than at the not-recorded `None` default. The POI kind
    # set is stamped here for the same reason and in the same breath: this function IS
    # where `poi.classify` runs, so the registry it was classified against is exactly the
    # one this build has. Recording it anywhere else would be a second source of truth.
    area = AreaData(
        transit=[], poi_kinds=_poi.all_kinds(), ferrata_routes=[], ferrata_ways=[]
    )
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
    # A cabled way matches BOTH ferrata clauses when it carries a grade and the highway
    # value, and Overpass emits an element once per matching statement.
    seen_ferrata_ways: set[object] = set()
    for el in elements:
        if _is_tag_only_way(el):
            continue  # already harvested into way_tags above
        tags = el.get("tags", {}) or {}
        route_kind = tags.get("route") if el.get("type") == "relation" else None
        if route_kind in ("hiking", "foot", _ferrata.FERRATA_ROUTE):
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
            # `route=via_ferrata` is measured exactly like a hiking route — same dict,
            # same downstream machinery — but filed apart, so it can only ever surface
            # through a search that asked for it. See AreaData.ferrata_routes.
            bucket = (
                area.ferrata_routes
                if route_kind == _ferrata.FERRATA_ROUTE
                else area.routes
            )
            bucket.append(
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
        elif el.get("type") == "way" and _ferrata.way_is_ferrata(tags):
            # ABOVE the POI branch for the same reason the access branches are: a cabled
            # way that also carries a registry tag is a hazard first. It keeps its full
            # geometry (`out geom`) because a ferrata is a line you walk, not a point —
            # unlike parking or a transit stop, a representative coordinate would say
            # nothing useful about where the cable actually runs.
            geom = el.get("geometry") or []
            ident = el.get("id")
            if geom and ident not in seen_ferrata_ways:
                seen_ferrata_ways.add(ident)
                area.ferrata_ways.append(
                    {
                        "id": ident,
                        "coords": [(pt["lat"], pt["lon"]) for pt in geom],
                        "name": tags.get("name"),
                        "scale": _ferrata.scale_of(tags),
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
