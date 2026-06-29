"""Standalone local web UI — find hikes in a browser, no MCP client or LLM.

Pure standard library (``http.server``) — no web framework dependency. Serves a
Leaflet map: pan/zoom to your area, set filters, click "Search this map area",
and matching routes are listed and pinned at their start point. This is the
friendly answer to "how do I get a bounding box" — you draw it by moving the map.

Run::

    hike-finder-web        # then open http://127.0.0.1:8765

Same engine as the CLI and the MCP server (see search.search_hikes).
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .export import GEOJSON_MIME, GPX_MIME, hikes_to_geojson, hikes_to_gpx
from .filters import Criteria
from .format import hike_to_dict
from .search import compose_loops, download_area, search_hikes, search_snapshot
from .snapshot import default_snapshot_dir, load_snapshot, save_snapshot

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hike-finder</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  body { margin:0; font:14px/1.45 system-ui, sans-serif; color:#222; }
  #app { display:flex; height:100vh; }
  #map { flex:1; }
  #side { width:370px; padding:14px 16px; overflow:auto; border-left:1px solid #ddd; box-sizing:border-box; }
  h1 { font-size:18px; margin:0 0 4px; }
  label { display:block; margin:10px 0 2px; font-weight:600; }
  input, select { width:100%; padding:5px 6px; box-sizing:border-box; }
  .row { display:flex; gap:8px; }
  .row > div { flex:1; }
  button { margin-top:14px; width:100%; padding:9px; font-weight:600; cursor:pointer; }
  #status { margin-top:10px; color:#555; min-height:1.2em; }
  .hike { border-top:1px solid #eee; padding:9px 0; cursor:pointer; }
  .hike:hover { background:#f6f8fa; }
  .hike .name { font-weight:600; }
  .hike .meta { color:#444; }
  .flags span { display:inline-block; background:#eef; border-radius:3px; padding:0 6px; margin:3px 4px 0 0; font-size:12px; }
  .muted { color:#888; font-size:12px; }
  .hike.near { background:#fffaf0; }
  .hike.near .name::before { content:"~ "; color:#b8860b; }
  .note { color:#a06000; font-size:12px; margin-top:2px; }
</style>
</head>
<body>
<div id="app">
  <div id="map"></div>
  <div id="side">
    <h1>hike-finder</h1>
    <p class="muted">Pan/zoom to your area, set filters, then search. Data: OpenStreetMap. No LLM involved.</p>

    <label>Contact (email or URL) <span class="muted">— recommended</span></label>
    <input id="ua" placeholder="you@example.com">

    <label>Search area</label>
    <select id="area">
      <option value="">— live map (fetches OSM) —</option>
    </select>
    <div class="row" style="margin-top:6px;">
      <div style="flex:2;"><input id="area_name" placeholder="name this view, e.g. krkonose"></div>
      <div style="flex:1;"><button id="download" style="margin-top:0;" title="Fetch this map view once and save it for offline, API-free searching">Download view</button></div>
    </div>
    <p class="muted">Download a view once, then pick it above to search offline with no API calls.</p>

    <label>Shape</label>
    <select id="circular">
      <option value="">any</option><option value="true">loops only</option><option value="false">point-to-point only</option>
    </select>

    <label>Car access (parking near an end)</label>
    <select id="car_access">
      <option value="">any</option><option value="true">required</option><option value="false">excluded</option>
    </select>

    <label>Chairlift access (lift near an end)</label>
    <select id="chairlift_access">
      <option value="">any</option><option value="true">required</option><option value="false">excluded</option>
    </select>

    <label>Near misses (close-but-not-matching routes)</label>
    <select id="near_misses">
      <option value="">auto (show only if nothing matches)</option>
      <option value="true">always show</option>
      <option value="false">never show</option>
    </select>

    <div class="row">
      <div><label>Min gain (m)</label><input id="min_gain_m" type="number"></div>
      <div><label>Max gain (m)</label><input id="max_gain_m" type="number"></div>
    </div>
    <div class="row">
      <div><label>Min dist (km)</label><input id="min_distance_km" type="number" step="0.1"></div>
      <div><label>Max dist (km)</label><input id="max_distance_km" type="number" step="0.1"></div>
    </div>

    <label style="margin-top:12px;"><input type="checkbox" id="compose_loops" style="width:auto; vertical-align:middle;"> Compose loops from connected trails</label>
    <p class="muted">Stitch several marked trails into day-loops of your target distance (uses min/max dist above; default 3–15 km). Live map only.</p>

    <label><input type="checkbox" id="name_places" style="width:auto; vertical-align:middle;"> Name unnamed routes from places</label>
    <p class="muted">Label routes with no OSM name (route/&lt;id&gt;) from their endpoints' place names, e.g. “Pec → Sněžka”, via Nominatim. Live map only (needs the network).</p>

    <button id="search">Search this map area</button>
    <div class="row" style="margin-top:8px;">
      <div><button id="dl_gpx" style="margin-top:0;" title="Download the listed routes as a GPX track for your GPS / phone">Download GPX</button></div>
      <div><button id="dl_geojson" style="margin-top:0;" title="Download the listed routes as GeoJSON">Download GeoJSON</button></div>
    </div>
    <p class="muted">Download the routes you see — load the GPX into Komoot / OsmAnd / mapy.cz / a Garmin.</p>
    <div id="status"></div>
    <div id="results"></div>
  </div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const map = L.map('map').setView([50.73, 15.60], 13);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
const markers = L.layerGroup().addTo(map);
const routeLines = L.layerGroup().addTo(map);   // drawn polylines for the matched routes
let lastParams = null;                           // params of the last search, for GPX/GeoJSON download

function esc(s){ return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function val(id){ const v = document.getElementById(id).value.trim(); return v === '' ? null : v; }

const FIELDS = ['circular','car_access','chairlift_access','near_misses',
                'min_gain_m','max_gain_m','min_distance_km','max_distance_km','user_agent'];

async function loadAreas(selectName){
  // Populate the saved-area selector from disk, preserving the live-map option.
  try {
    const areas = await (await fetch('/api/areas')).json();
    const sel = document.getElementById('area');
    sel.length = 1;  // keep the first "live map" option
    for (const a of areas){
      const o = document.createElement('option');
      o.value = a.name;
      o.textContent = a.name + ' (' + a.routes + ' routes)';
      sel.appendChild(o);
    }
    if (selectName) sel.value = selectName;
  } catch (e){ /* best-effort */ }
}

async function search(){
  const area = document.getElementById('area').value;
  const params = new URLSearchParams();
  if (area){
    params.set('area', area);                 // offline: bbox comes from the snapshot
  } else {
    const b = map.getBounds();
    params.set('south', b.getSouth()); params.set('west', b.getWest());
    params.set('north', b.getNorth()); params.set('east', b.getEast());
    // Loop composition is a live-map-only mode (it builds a graph from fetched OSM).
    if (document.getElementById('compose_loops').checked) params.set('compose_loops', 'true');
  }
  // Reverse-geocode naming of unnamed routes — applies live (offline it's a no-op).
  if (document.getElementById('name_places').checked) params.set('name_places', 'true');
  for (const f of FIELDS){ const id = (f === 'user_agent') ? 'ua' : f; const v = val(id); if (v !== null) params.set(f, v); }
  // Remember exactly what we searched, so the GPX/GeoJSON download reproduces THIS
  // result set (the same area/bbox/filters) rather than the current map view.
  lastParams = params.toString();

  const status = document.getElementById('status');
  const results = document.getElementById('results');
  status.textContent = area ? ('Searching “' + area + '” offline…') : 'Searching…';
  results.innerHTML = ''; markers.clearLayers(); routeLines.clearLayers();
  try {
    const resp = await fetch('/api/hikes?' + params.toString());
    const data = await resp.json();
    if (!resp.ok || data.error){ status.textContent = 'Error: ' + (data.error || resp.status); return; }
    render(data);
    const near = data.filter(h => h.near_miss).length;
    const composing = !area && document.getElementById('compose_loops').checked;
    const anchored = composing && (document.getElementById('car_access').value === 'true'
                                || document.getElementById('chairlift_access').value === 'true');
    if (composing && data.length === 0){
      status.textContent = anchored
        ? 'No loops here reachable from a parking lot / lift — clear the car/lift requirement, or widen the map or distance band.'
        : 'No loops could be composed here — widen the map or the min/max distance.';
    } else {
      status.textContent = (data.length - near) + (composing ? ' loop(s)' : ' match(es)')
        + (near ? (' + ' + near + ' near miss(es)') : '') + (area ? ' [offline]' : '');
    }
    if (!area) showQuota();
  } catch (e){ status.textContent = 'Request failed: ' + e; }
}

async function downloadArea(){
  const name = (document.getElementById('area_name').value || '').trim();
  if (!name){ document.getElementById('status').textContent = 'Enter a name for this view first.'; return; }
  const b = map.getBounds();
  const params = new URLSearchParams({
    name, south: b.getSouth(), west: b.getWest(), north: b.getNorth(), east: b.getEast()
  });
  const ua = val('ua'); if (ua !== null) params.set('user_agent', ua);
  // Reuse the naming checkbox: when checked, bake place names into the snapshot so an
  // offline search of it can label unnamed routes (otherwise that's a no-op offline).
  const naming = document.getElementById('name_places').checked;
  if (naming) params.set('name_places', 'true');
  const status = document.getElementById('status');
  status.textContent = 'Downloading “' + name + '” (one-time fetch + elevation'
    + (naming ? ' + place names' : '') + ')…';
  try {
    const resp = await fetch('/api/download?' + params.toString());
    const data = await resp.json();
    if (!resp.ok || data.error){ status.textContent = 'Error: ' + (data.error || resp.status); return; }
    status.textContent = 'Saved “' + data.name + '”: ' + data.routes + ' routes, '
      + data.samples + ' elevation samples'
      + (naming ? (', ' + (data.places || 0) + ' baked place names') : '')
      + '. Now searchable offline.';
    await loadAreas(data.name);
    showQuota();
  } catch (e){ status.textContent = 'Download failed: ' + e; }
}

async function showQuota(){
  // Separate, non-blocking call so the daily-cap counter never reshapes the
  // hikes response. Appended to the status line; silent if disabled/unavailable.
  try {
    const q = await (await fetch('/api/quota')).json();
    if (q && q.enabled){
      document.getElementById('status').textContent +=
        '  ·  elevation API: ' + q.used + '/' + q.limit + ' requests today';
    }
  } catch (e){ /* counter is best-effort; ignore */ }
}

function render(hikes){
  const results = document.getElementById('results');
  hikes.forEach(h => {
    // An unnamed route shows its reverse-geocoded place label when one was derived;
    // otherwise the truthful name (a real OSM name, or the route/<id> fallback).
    const dispName = h.place_name || h.name;
    const marker = L.marker([h.start.lat, h.start.lon]).addTo(markers)
      .bindPopup('<b>' + esc(dispName) + '</b><br>' + h.distance_km + ' km'
                 + (h.gain_m != null ? (', +' + h.gain_m + ' m') : ''));
    // Draw the route line(s): near-miss amber, composed loop dashed purple, else blue.
    // geometry is a list of member ways, each an array of [lat, lon] points.
    const color = h.near_miss ? '#d08700' : (h.composed ? '#7048e8' : '#2563eb');
    const lines = [];
    (h.geometry || []).forEach(way => {
      if (way && way.length >= 2){
        const pl = L.polyline(way, { color, weight: 4, opacity: 0.8,
                                     dashArray: h.composed ? '6 5' : null }).addTo(routeLines);
        pl.bindPopup('<b>' + esc(dispName) + '</b><br>' + h.distance_km + ' km'
                     + (h.gain_m != null ? (', +' + h.gain_m + ' m / -' + h.loss_m + ' m') : ''));
        lines.push(pl);
      }
    });
    const flags = [ h.circular ? 'loop' : 'one-way' ];
    if (h.car_access) flags.push('car');
    if (h.chairlift_access) flags.push('lift:' + esc(h.lift_type));
    const gain = (h.gain_m != null) ? ('+' + h.gain_m + ' m / -' + h.loss_m + ' m') : 'gain n/a';
    const note = (h.near_miss && h.notes && h.notes.length)
      ? '<div class="note">near miss: ' + esc(h.notes.join('; ')) + '</div>' : '';
    // A composed loop has no single relation id — name its constituent trails. An
    // unnamed route given a place label is marked "unnamed OSM relation" so the
    // geocoded label is never mistaken for the route's signed trail name.
    const ident = h.composed
      ? ('composed of ' + esc((h.composed_of || []).join(' + ')))
      : ((h.place_name ? 'unnamed OSM relation ' : 'OSM relation ') + h.osm_id);
    const el = document.createElement('div');
    el.className = h.near_miss ? 'hike near' : 'hike';
    el.innerHTML = '<div class="name">' + esc(dispName) + '</div>'
      + '<div class="meta">' + h.distance_km + ' km &middot; ' + gain + '</div>'
      + '<div class="flags">' + flags.map(f => '<span>' + f + '</span>').join('') + '</div>'
      + note
      + '<div class="muted">' + ident + '</div>';
    el.onclick = () => {
      // Frame the whole route if we drew it; otherwise just centre on the start.
      if (lines.length){
        try { map.fitBounds(L.featureGroup(lines).getBounds().pad(0.2)); }
        catch(e){ map.setView([h.start.lat, h.start.lon], 15); }
      } else {
        map.setView([h.start.lat, h.start.lon], 15);
      }
      marker.openPopup();
    };
    results.appendChild(el);
  });
}

function download(fmt){
  // Re-runs the LAST search server-side and streams the file (cache-hot live, free
  // offline). Uses the stored params so the download matches what's listed.
  const status = document.getElementById('status');
  if (!lastParams){ status.textContent = 'Search first, then download GPX/GeoJSON.'; return; }
  window.location = '/api/' + fmt + '?' + lastParams;
}
document.getElementById('search').onclick = search;
document.getElementById('download').onclick = downloadArea;
document.getElementById('dl_gpx').onclick = () => download('gpx');
document.getElementById('dl_geojson').onclick = () => download('geojson');
loadAreas();
</script>
</body>
</html>
"""


def _tri(qs: dict, key: str) -> bool | None:
    v = qs.get(key, [None])[0]
    if v is None or v == "":
        return None
    return v.lower() in ("true", "1", "yes", "on")


def _num(qs: dict, key: str) -> float | None:
    v = qs.get(key, [None])[0]
    if v is None or v == "":
        return None
    return float(v)


def _str(qs: dict, key: str) -> str | None:
    v = qs.get(key, [None])[0]
    return v or None


def _slug(name: str) -> str:
    """A safe snapshot filename stem: keep word chars and dashes, never a path."""
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in name).strip("_")


def _snapshot_path(name: str):
    stem = _slug(name)
    if not stem:
        return None
    return default_snapshot_dir() / f"{stem}.json"


def _list_areas() -> list[dict]:
    """Light metadata for every saved snapshot (no full elevation load)."""
    out = []
    d = default_snapshot_dir()
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(
            {
                "name": path.stem,
                "bbox": data.get("bbox"),
                "created_at": data.get("created_at"),
                "routes": len(data.get("area", {}).get("routes", [])),
                "samples": len(data.get("elevations", {})),
            }
        )
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body: str, ctype: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/hikes":
            self._api(parse_qs(parsed.query))
            return
        if parsed.path == "/api/gpx":
            self._export(parse_qs(parsed.query), "gpx")
            return
        if parsed.path == "/api/geojson":
            self._export(parse_qs(parsed.query), "geojson")
            return
        if parsed.path == "/api/areas":
            self._areas()
            return
        if parsed.path == "/api/download":
            self._download(parse_qs(parsed.query))
            return
        if parsed.path == "/api/quota":
            self._quota()
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _areas(self) -> None:
        self._json(200, _list_areas())

    def _download(self, qs: dict) -> None:
        name = _str(qs, "name")
        path = _snapshot_path(name) if name else None
        if path is None:
            self._json(400, {"error": "a non-empty area name is required"})
            return
        try:
            bbox = (
                float(qs["south"][0]),
                float(qs["west"][0]),
                float(qs["north"][0]),
                float(qs["east"][0]),
            )
        except (KeyError, ValueError):
            self._json(400, {"error": "south/west/north/east are required"})
            return
        # Opt-in (same checkbox as the live search): also bake reverse-geocoded names
        # for the unnamed routes so an offline search of this snapshot can label them.
        name_places = _tri(qs, "name_places")
        try:
            snap = download_area(
                bbox, user_agent=_str(qs, "user_agent"), name_places=name_places
            )
            save_snapshot(snap, path)
        except Exception as e:  # noqa: BLE001 — surface any fetch/write failure to the UI
            msg = str(e)
            if "406" in msg:
                msg += " — fill in the Contact field (the public Overpass server rejects the default User-Agent)."
            self._json(502, {"error": f"download failed: {msg}"})
            return
        self._json(200, {
            "name": path.stem, "routes": snap.route_count,
            "samples": snap.sample_count, "places": snap.place_count,
        })

    def _quota(self) -> None:
        # Separate endpoint so we never reshape /api/hikes (a bare array the JS
        # iterates) just to attach the counter.
        from . import config as _config
        from .elevation import api_quota_snapshot

        used, limit = api_quota_snapshot(_config.load())
        body = json.dumps(
            {
                "used": used,
                "limit": limit,
                "remaining": (limit - used) if limit > 0 else None,
                "enabled": limit > 0,
            }
        )
        self._send(200, body, "application/json; charset=utf-8")

    def _resolve_hikes(self, qs: dict):
        """Run the search a query describes (offline --area or live bbox/compose).

        Shared by ``/api/hikes`` (JSON) and ``/api/gpx`` / ``/api/geojson`` (file
        download) so all three agree on filters, area resolution, and error handling.
        Returns ``(hikes, None)`` on success or ``(None, (status, {"error": ...}))``.
        """
        criteria = Criteria(
            min_gain_m=_num(qs, "min_gain_m"),
            max_gain_m=_num(qs, "max_gain_m"),
            min_distance_km=_num(qs, "min_distance_km"),
            max_distance_km=_num(qs, "max_distance_km"),
            circular=_tri(qs, "circular"),
            car_access=_tri(qs, "car_access"),
            chairlift_access=_tri(qs, "chairlift_access"),
        )
        # near_misses tri-state: absent -> "auto", true -> always, false -> never.
        nm = _tri(qs, "near_misses")
        near_miss = "auto" if nm is None else nm
        # Reverse-geocode naming of unnamed routes (opt-in checkbox; off by default).
        name_places = _tri(qs, "name_places")

        area_name = _str(qs, "area")
        if area_name:
            # Offline: search a saved snapshot — no network, no API calls.
            path = _snapshot_path(area_name)
            if path is None or not path.is_file():
                return None, (404, {"error": f"no saved area named {area_name!r}"})
            try:
                snap = load_snapshot(path)
                return search_snapshot(
                    snap, criteria, near_miss=near_miss, name_places=name_places
                ), None
            except (OSError, ValueError) as e:
                return None, (500, {"error": f"could not search snapshot: {e}"})

        try:
            bbox = (
                float(qs["south"][0]),
                float(qs["west"][0]),
                float(qs["north"][0]),
                float(qs["east"][0]),
            )
        except (KeyError, ValueError):
            return None, (400, {"error": "south/west/north/east are required"})

        # Loop composition: synthesise loops from connected trails inside the box.
        composing = _tri(qs, "compose_loops")
        search = compose_loops if composing else search_hikes
        kwargs = dict(user_agent=_str(qs, "user_agent"), near_miss=near_miss)
        # Naming applies only to ordinary routes — composed loops already carry their
        # constituent-trail label, never a route/<id> fallback.
        if not composing:
            kwargs["name_places"] = name_places
        try:
            hikes = search(bbox, criteria, **kwargs)
        except Exception as e:
            msg = str(e)
            if "406" in msg:
                msg += (
                    " — fill in the Contact field (the public Overpass server rejects "
                    "the default User-Agent)."
                )
            return None, (502, {"error": f"failed to fetch OSM data: {msg}"})
        return hikes, None

    def _api(self, qs: dict) -> None:
        hikes, err = self._resolve_hikes(qs)
        if err is not None:
            self._json(*err)
            return
        # geometry=True so the map can draw the route lines without a second search.
        self._json(200, [hike_to_dict(h, geometry=True) for h in hikes])

    def _export(self, qs: dict, fmt: str) -> None:
        """Run the query's search and stream the results as a GPX / GeoJSON download."""
        hikes, err = self._resolve_hikes(qs)
        if err is not None:
            self._json(*err)
            return
        if fmt == "gpx":
            body, mime, filename = hikes_to_gpx(hikes), GPX_MIME, "hikes.gpx"
        else:
            body, mime, filename = hikes_to_geojson(hikes), GEOJSON_MIME, "hikes.geojson"
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="hike-finder-web",
        description="Local web UI for hike-finder (map + filters). No LLM or MCP client required.",
    )
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1).")
    p.add_argument("--port", type=int, default=8765, help="Port (default 8765).")
    args = p.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"hike-finder web UI on {url}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
