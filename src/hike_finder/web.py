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

from .filters import Criteria
from .format import hike_to_dict
from .search import search_hikes

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

    <div class="row">
      <div><label>Min gain (m)</label><input id="min_gain_m" type="number"></div>
      <div><label>Max gain (m)</label><input id="max_gain_m" type="number"></div>
    </div>
    <div class="row">
      <div><label>Min dist (km)</label><input id="min_distance_km" type="number" step="0.1"></div>
      <div><label>Max dist (km)</label><input id="max_distance_km" type="number" step="0.1"></div>
    </div>

    <button id="search">Search this map area</button>
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

function esc(s){ return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function val(id){ const v = document.getElementById(id).value.trim(); return v === '' ? null : v; }

const FIELDS = ['circular','car_access','chairlift_access',
                'min_gain_m','max_gain_m','min_distance_km','max_distance_km','user_agent'];

async function search(){
  const b = map.getBounds();
  const params = new URLSearchParams({
    south: b.getSouth(), west: b.getWest(), north: b.getNorth(), east: b.getEast()
  });
  for (const f of FIELDS){ const id = (f === 'user_agent') ? 'ua' : f; const v = val(id); if (v !== null) params.set(f, v); }

  const status = document.getElementById('status');
  const results = document.getElementById('results');
  status.textContent = 'Searching…'; results.innerHTML = ''; markers.clearLayers();
  try {
    const resp = await fetch('/api/hikes?' + params.toString());
    const data = await resp.json();
    if (!resp.ok || data.error){ status.textContent = 'Error: ' + (data.error || resp.status); return; }
    render(data);
    status.textContent = data.length + ' hike(s) found.';
  } catch (e){ status.textContent = 'Request failed: ' + e; }
}

function render(hikes){
  const results = document.getElementById('results');
  hikes.forEach(h => {
    const marker = L.marker([h.start.lat, h.start.lon]).addTo(markers)
      .bindPopup('<b>' + esc(h.name) + '</b><br>' + h.distance_km + ' km'
                 + (h.gain_m != null ? (', +' + h.gain_m + ' m') : ''));
    const flags = [ h.circular ? 'loop' : 'one-way' ];
    if (h.car_access) flags.push('car');
    if (h.chairlift_access) flags.push('lift:' + esc(h.lift_type));
    const gain = (h.gain_m != null) ? ('+' + h.gain_m + ' m / -' + h.loss_m + ' m') : 'gain n/a';
    const el = document.createElement('div');
    el.className = 'hike';
    el.innerHTML = '<div class="name">' + esc(h.name) + '</div>'
      + '<div class="meta">' + h.distance_km + ' km &middot; ' + gain + '</div>'
      + '<div class="flags">' + flags.map(f => '<span>' + f + '</span>').join('') + '</div>'
      + '<div class="muted">OSM relation ' + h.osm_id + '</div>';
    el.onclick = () => { map.setView([h.start.lat, h.start.lon], 15); marker.openPopup(); };
    results.appendChild(el);
  });
}
document.getElementById('search').onclick = search;
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
        self._send(404, "not found", "text/plain; charset=utf-8")

    def _api(self, qs: dict) -> None:
        try:
            bbox = (
                float(qs["south"][0]),
                float(qs["west"][0]),
                float(qs["north"][0]),
                float(qs["east"][0]),
            )
        except (KeyError, ValueError):
            self._send(
                400,
                json.dumps({"error": "south/west/north/east are required"}),
                "application/json; charset=utf-8",
            )
            return

        criteria = Criteria(
            min_gain_m=_num(qs, "min_gain_m"),
            max_gain_m=_num(qs, "max_gain_m"),
            min_distance_km=_num(qs, "min_distance_km"),
            max_distance_km=_num(qs, "max_distance_km"),
            circular=_tri(qs, "circular"),
            car_access=_tri(qs, "car_access"),
            chairlift_access=_tri(qs, "chairlift_access"),
        )
        try:
            hikes = search_hikes(bbox, criteria, user_agent=_str(qs, "user_agent"))
        except Exception as e:
            msg = str(e)
            if "406" in msg:
                msg += (
                    " — fill in the Contact field (the public Overpass server rejects "
                    "the default User-Agent)."
                )
            self._send(
                502,
                json.dumps({"error": f"failed to fetch OSM data: {msg}"}),
                "application/json; charset=utf-8",
            )
            return

        body = json.dumps([hike_to_dict(h) for h in hikes], ensure_ascii=False)
        self._send(200, body, "application/json; charset=utf-8")


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
