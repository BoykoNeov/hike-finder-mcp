"""Fetch hiking routes — plus parking and chairlift features — from OSM.

We target route RELATIONS (route=hiking/foot), not raw highway=path ways.
Relations are the signed, named, maintained trails — including the Czech KČT
network that mapy.cz renders — which is what gives results the "mapy.cz feel"
instead of every unmarked path.

In ONE Overpass round-trip we also pull the features the new filters need:
  - amenity=parking  (car access; ``out center`` gives a representative coord)
  - ride-up aerialways (chairlift access; ``out geom`` gives station endpoints)

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

import requests

from .access import RIDE_UP_AERIALWAYS

Coord = tuple[float, float]

DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# overpass-api.de sits behind Apache/mod_security, which rejects the default
# python-requests User-Agent with "406 Not Acceptable" before the query is even
# parsed. A descriptive UA is REQUIRED, not optional — confirmed live. Set a real
# contact via HIKE_OVERPASS_UA (wired through config.py -> server.py) per OSM
# etiquette; this default works but names no contact.
USER_AGENT = (
    "hike-finder-mcp/0.1 (OSM hiking route search; set HIKE_OVERPASS_UA with your contact)"
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


def build_query(
    south: float, west: float, north: float, east: float, timeout_s: int = 60
) -> str:
    """Overpass QL: hiking routes + parking + ride-up aerialways in the bbox."""
    bbox = f"{south},{west},{north},{east}"
    lift_re = "|".join(sorted(RIDE_UP_AERIALWAYS))
    return f"""
    [out:json][timeout:{timeout_s}];
    (
      relation["route"="hiking"]({bbox});
      relation["route"="foot"]({bbox});
    );
    out body geom;
    nwr["amenity"="parking"]({bbox});
    out center;
    way["aerialway"~"^({lift_re})$"]({bbox});
    out geom;
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


def parse_area(elements: list[dict]) -> AreaData:
    """Pure: split a mixed Overpass element list into routes/parking/lifts.

    Branching is by tag, not element type, so a parking *way* and an aerialway
    *way* never collide. This is the failure-prone part of the network layer,
    so it lives here, isolated and unit-tested without a live endpoint.
    """
    area = AreaData()
    for el in elements:
        tags = el.get("tags", {}) or {}
        if el.get("type") == "relation" and tags.get("route") in ("hiking", "foot"):
            ways: list[list[Coord]] = []
            for member in el.get("members", []):
                if member.get("type") == "way" and "geometry" in member:
                    ways.append([(pt["lat"], pt["lon"]) for pt in member["geometry"]])
            if not ways:
                continue
            area.routes.append(
                {
                    "id": el.get("id"),
                    "name": tags.get("name") or tags.get("ref") or f"route/{el.get('id')}",
                    "ref": tags.get("ref"),
                    "osmc_color": tags.get("osmc:symbol"),  # KČT marking, if present
                    "tags": tags,
                    "ways": ways,
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
    resp.raise_for_status()

    elements = resp.json().get("elements", [])
    return parse_area(elements)
