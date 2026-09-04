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

from . import config as _config
from .export import (
    GEOJSON_MIME,
    GPX_MIME,
    hikes_to_geojson,
    hikes_to_gpx,
    pois_to_geojson,
    pois_to_gpx,
)
from .filters import Criteria
from .format import format_poi_summary, hike_to_dict
from .poi import kind_labels, normalise_kinds, unrecorded_kinds
from .search import (
    area_has_no_routes,
    compose_loops,
    compose_loops_around,
    download_area,
    ferrata_gap_message,
    list_area_pois,
    list_snapshot_pois,
    no_routes_message,
    route_via,
    routes_between,
    routes_to_poi,
    search_hikes,
    search_snapshot,
    snapshot_kinds_missing_message,
    snapshot_poi_gap,
)
from .snapshot import list_snapshots, load_snapshot, save_snapshot, snapshot_path

# What a live listing (and every error path) reports about POI coverage: the fetch just
# happened against this build's registry, so nothing is missing and nothing is unknown.
_GAP_OK = {"state": "ok", "kinds": [], "message": ""}

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
  /* What the SOURCE could not answer — kept out of #status so it survives a result set
     that isn't empty. An empty list is only the loudest symptom of these gaps, not the
     trigger for saying them (see web.py's _area_notices). */
  .notice { margin-top:8px; padding:7px 9px; border-left:3px solid #d08700;
            background:#fffaf0; color:#7a4b00; font-size:12px; }
  .hike { border-top:1px solid #eee; padding:9px 0; cursor:pointer; }
  .hike:hover { background:#f6f8fa; }
  .hike .name { font-weight:600; }
  .hike .meta { color:#444; }
  .flags span { display:inline-block; background:#eef; border-radius:3px; padding:0 6px; margin:3px 4px 0 0; font-size:12px; }
  .muted { color:#888; font-size:12px; }
  .hike.near { background:#fffaf0; }
  .hike.near .name::before { content:"~ "; color:#b8860b; }
  .note { color:#a06000; font-size:12px; margin-top:2px; }
  .passes { color:#0b6b4f; font-size:12px; margin-top:2px; }
  select[multiple] { height:auto; padding:3px; }
  .area { border:1px solid #e3e6ea; border-radius:4px; padding:6px 8px; margin-top:6px; cursor:pointer; }
  .area:hover { background:#f6f8fa; border-color:#c9d2dc; }
  .area.on { background:#eef6ff; border-color:#7aa7d9; }
  .area .an { font-weight:600; }
  .area .ad { color:#666; font-size:12px; }
  .area .warn { color:#a06000; font-size:12px; }
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

    <label>Area to fetch / search</label>
    <div class="row">
      <div><button id="draw" style="margin-top:0;" title="Drag a box on the map to pick an exact area">Draw a box</button></div>
      <div><button id="draw_clear" style="margin-top:0;" title="Go back to using whatever the map is showing">Use whole view</button></div>
    </div>
    <p class="muted" id="sel_note"></p>
    <div class="row" style="margin-top:6px;">
      <div style="flex:2;"><input id="area_name" placeholder="name this area, e.g. krkonose"></div>
      <div style="flex:1;"><button id="download" style="margin-top:0;" title="Fetch this area once and save it for offline, API-free searching">Download</button></div>
    </div>

    <label>Already downloaded <span class="muted" id="areas_count"></span></label>
    <p class="muted">Outlined on the map. Click one to search it offline — no network, no API calls.</p>
    <div id="areas_list"></div>

    <label>Mode</label>
    <select id="mode">
      <option value="area">Search this map area</option>
      <option value="around">Circular routes near a point</option>
      <option value="between">Routes between two points</option>
      <option value="via">Route linking several points</option>
      <option value="topoi">Route to the nearest church / ruin / peak…</option>
      <option value="pois">Show points of interest (no routes)</option>
    </select>
    <div id="around_ctl" style="display:none;">
      <p class="muted">Click the map to drop your point. Loops passing within the radius of it (using the min/max distance below, default 3–15 km) are drawn, each starting there. Live map only.</p>
      <label>Near-point radius (m)</label>
      <input id="around_radius_m" type="number" step="50" value="1000">
    </div>
    <div id="between_ctl" style="display:none;">
      <p class="muted">Click the map to drop your <b>start</b>, then your <b>finish</b>. The shortest routes between them are drawn, shortest first. Live map only.</p>
      <label>How many routes</label>
      <input id="routes_k" type="number" step="1" min="1" value="3">
    </div>
    <div id="via_ctl" style="display:none;">
      <p class="muted">Click the map to drop <b>waypoints</b> (2 or more). They are linked into ONE route in the order you click them, each snapped to the nearest trail. Live map only.</p>
      <label><input type="checkbox" id="via_loop" style="width:auto; vertical-align:middle;"> Close into a circular route</label>
      <p class="muted">Return to the first point by a different way where the trail network allows, so the loop avoids retracing itself.</p>
      <button id="via_undo" style="margin-top:0;" title="Remove the last waypoint you dropped">Undo last point</button>
    </div>
    <div id="topoi_ctl" style="display:none;">
      <p class="muted">Click the map to drop your <b>start</b>, pick what to walk to, and the routes to the nearest ones are drawn — nearest <b>along the trails</b>, not as the crow flies. Each route ends at the closest point on a trail, and how far that lands from the object is shown. Live map only.</p>
      <label>Walk to</label>
      <select id="to_poi" multiple size="7"></select>
      <p class="muted">Ctrl/&#8984;-click for several — the nearest of <b>any</b> of them. This is the opposite of “Must pass” below: that one filters routes you already found, this one draws the route to the object.</p>
      <div class="row">
        <div><label>How many</label><input id="to_poi_n" type="number" step="1" min="1" value="3"></div>
        <div><label>Look within (m)</label><input id="to_poi_radius_m" type="number" step="500" placeholder="3000"></div>
      </div>
      <p class="muted">“Look within” also sizes the area fetched, so raising it makes the query heavier — but it is the lever when nothing of the kind is found nearby.</p>
    </div>
    <div id="pois_ctl" style="display:none;">
      <p class="muted">Every object of the kinds you pick, pinned and listed — <b>no routes are drawn to them</b>. Works on the live map (your drawn box, else the view) <b>and</b> on a downloaded area, where it costs no network at all. No elevation is looked up either, so this spends nothing from the daily API budget.</p>
      <label>Show</label>
      <select id="show_poi" multiple size="7"></select>
      <p class="muted">Ctrl/&#8984;-click for several. Leave <b>nothing</b> selected to show every kind. Then use the download buttons to export them as GPX waypoints for your GPS / phone.</p>
    </div>

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

    <label>Public transport (station/stop near an end)</label>
    <select id="transit_access">
      <option value="">any</option><option value="true">required</option><option value="false">excluded</option>
    </select>

    <label>Via ferrata (cabled climbing)</label>
    <select id="ferrata">
      <option value="">any</option><option value="true">only ferrata</option><option value="false">exclude ferrata</option>
    </select>
    <p class="muted">A via ferrata is a climb on fixed steel cable, walked in a harness &mdash; not a harder hike. <b>Only ferrata</b> also returns dedicated <code>route=via_ferrata</code> relations no other search shows. <b>Exclude</b> drops routes <i>known</i> to include cable, from OSM tags &mdash; a filter, <b>not a safety guarantee</b>: untagged cable cannot be detected.</p>

    <label>Must pass (churches, ruins, peaks…)</label>
    <select id="poi" multiple size="7"></select>
    <p class="muted">Ctrl/&#8984;-click for several — a route passing <b>any</b> of them is kept, and what it reaches is listed with the distance. Leave empty for no destination filter.</p>
    <label>How close counts (m)</label>
    <input id="poi_radius_m" type="number" step="50" placeholder="250">

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
      <div><button id="dl_gpx" style="margin-top:0;" title="Download what is listed — routes as GPX tracks, points of interest as GPX waypoints">Download GPX</button></div>
      <div><button id="dl_geojson" style="margin-top:0;" title="Download what is listed as GeoJSON">Download GeoJSON</button></div>
    </div>
    <p class="muted">Download what you see — routes as GPX tracks, or (in the “Show points of interest” mode) the objects as GPX waypoints. Load either into Komoot / OsmAnd / mapy.cz / a Garmin.</p>
    <div id="status"></div>
    <div id="notices"></div>
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
const picks = L.layerGroup().addTo(map);        // the point(s) the user clicked to pick
const poiMarkers = L.layerGroup().addTo(map);   // the objects the listed routes reach
const areaBoxes = L.layerGroup().addTo(map);    // outlines of the already-downloaded areas
let lastParams = null;                           // params of the last search, for GPX/GeoJSON download
let aroundPt = null, fromPt = null, toPt = null; // picked points for the point-based modes
let viaPts = [];                                 // ordered waypoints for the 'via' mode
let toPoiPt = null;                              // start point for the 'route to the nearest…' mode

function modeName(){ return document.getElementById('mode').value; }

// ---- the drawn selection ---------------------------------------------------------
// ONE box, read by both the download and the search: "the selected area" has to mean
// the same thing to each, or you would download one rectangle and search another. With
// nothing drawn it falls back to the map view, which is how this UI always worked.
let drawnBounds = null;      // the box you dragged, or null for "the whole view"
let drawRect = null;         // its Leaflet rectangle
let arming = false;          // the Draw button is armed, waiting for a drag
let dragFrom = null;         // where the drag started
let swallowClick = false;    // don't let the drag's mouseup double as a point-pick

function selectionBounds(){ return drawnBounds || map.getBounds(); }

function boundsParams(params){
  const b = selectionBounds();
  params.set('south', b.getSouth()); params.set('west', b.getWest());
  params.set('north', b.getNorth()); params.set('east', b.getEast());
}

function updateSel(){
  const note = document.getElementById('sel_note');
  const btn = document.getElementById('draw');
  if (arming){
    btn.textContent = 'Drawing…';
    note.textContent = 'Drag on the map to draw your area.';
  } else if (drawnBounds){
    const b = drawnBounds;
    // Rough size, so an accidental 300 m box or a reckless 200 km one is visible
    // BEFORE you spend an Overpass query and an elevation budget on it.
    const km = (a, c) => (map.distance(a, c) / 1000);
    const w = km([b.getSouth(), b.getWest()], [b.getSouth(), b.getEast()]);
    const h = km([b.getSouth(), b.getWest()], [b.getNorth(), b.getWest()]);
    btn.textContent = 'Redraw box';
    note.innerHTML = 'Using your drawn box, about <b>' + w.toFixed(1) + ' &times; '
      + h.toFixed(1) + ' km</b>. It is used for both downloading and searching.';
  } else {
    btn.textContent = 'Draw a box';
    note.textContent = 'Using the whole map view. Draw a box to pin an exact area — '
      + 'the same box is used for downloading and for searching.';
  }
}

function clearDraw(){
  drawnBounds = null;
  if (drawRect){ drawRect.remove(); drawRect = null; }
  arming = false;
  map.dragging.enable();
  map.getContainer().style.cursor = '';
  updateSel();
}

document.getElementById('draw').addEventListener('click', () => {
  arming = true;
  map.dragging.disable();          // otherwise the drag pans the map instead of drawing
  map.getContainer().style.cursor = 'crosshair';
  updateSel();
});
document.getElementById('draw_clear').addEventListener('click', clearDraw);

map.on('mousedown', e => {
  if (!arming) return;
  dragFrom = e.latlng;
  if (drawRect){ drawRect.remove(); drawRect = null; }
});
map.on('mousemove', e => {
  if (!arming || !dragFrom) return;
  const b = L.latLngBounds(dragFrom, e.latlng);
  if (drawRect) drawRect.setBounds(b);
  else drawRect = L.rectangle(b, { color:'#111', weight:2, dashArray:'5 4',
                                   fillColor:'#111', fillOpacity:0.05 }).addTo(map);
});
map.on('mouseup', e => {
  if (!arming || !dragFrom) return;
  const b = L.latLngBounds(dragFrom, e.latlng);
  dragFrom = null;
  arming = false;
  map.dragging.enable();
  map.getContainer().style.cursor = '';
  // The same gesture also fires a 'click', which must not double as a point-pick.
  // Guarded twice on purpose: `onMapClick` consumes the flag (so the suppression is
  // exact and does not depend on event-loop ordering), and this timer clears it if no
  // click follows at all — otherwise a stale flag would swallow the user's NEXT click.
  swallowClick = true;
  setTimeout(() => { swallowClick = false; }, 0);
  // A click with no real drag is a mis-hit, not a zero-size area — cancel it rather
  // than silently arming a search over a few metres of ground.
  if (map.distance(b.getSouthWest(), b.getNorthEast()) < 50){ clearDraw(); return; }
  drawnBounds = b;
  if (drawRect) drawRect.setBounds(b);
  updateSel();
});

function drawPicks(){
  // Redraw the picked-point markers for the current mode (a labelled circle each).
  picks.clearLayers();
  const dot = (pt, label, color) => L.circleMarker(pt, { radius:8, color, weight:3,
      fillColor:'#fff', fillOpacity:1 }).addTo(picks).bindTooltip(label, { permanent:true, direction:'right' });
  if (modeName() === 'around' && aroundPt) dot(aroundPt, 'point', '#7048e8');
  if (modeName() === 'between'){
    if (fromPt) dot(fromPt, 'start', '#188038');
    if (toPt) dot(toPt, 'finish', '#c5221f');
  }
  if (modeName() === 'via'){
    // Number the waypoints so their visiting order is visible on the map.
    viaPts.forEach((pt, i) => dot(pt, String(i + 1), '#1967d2'));
  }
  if (modeName() === 'topoi' && toPoiPt) dot(toPoiPt, 'start', '#188038');
}

function onMapClick(e){
  if (arming) return;                       // mid-draw: not a point-pick
  if (swallowClick){ swallowClick = false; return; }  // the drag's trailing click
  const m = modeName();
  if (m === 'around'){ aroundPt = e.latlng; }
  else if (m === 'between'){
    // First click (or a fresh pair) sets the start; the next sets the finish.
    if (!fromPt || (fromPt && toPt)){ fromPt = e.latlng; toPt = null; }
    else { toPt = e.latlng; }
  } else if (m === 'via'){ viaPts.push(e.latlng); }
  else if (m === 'topoi'){ toPoiPt = e.latlng; }
  else { return; }
  drawPicks();
  updateHint();
}
map.on('click', onMapClick);

function updateHint(){
  const s = document.getElementById('status');
  const m = modeName();
  if (m === 'around') s.textContent = aroundPt ? 'Point set — press Search.' : 'Click the map to drop your point.';
  else if (m === 'between') s.textContent = !fromPt ? 'Click the map to drop your start.'
      : (!toPt ? 'Now click your finish.' : 'Start + finish set — press Search.');
  else if (m === 'via') s.textContent = viaPts.length < 2
      ? ('Click the map to drop waypoints (' + viaPts.length + ' so far, need 2+).')
      : (viaPts.length + ' waypoints set — press Search (or keep adding).');
  else if (m === 'topoi') s.textContent = !toPoiPt
      ? 'Click the map to drop your start.'
      : (selectedToPois().length ? 'Start set — press Search.' : 'Now pick what to walk to.');
  else if (m === 'pois') s.textContent = document.getElementById('area').value
      ? 'Ready — press Show to list what is in the downloaded area.'
      : 'Ready — press Show to list what is in this map area.';
  else s.textContent = '';
}

function updateMode(){
  const m = modeName();
  document.getElementById('around_ctl').style.display = (m === 'around') ? 'block' : 'none';
  document.getElementById('between_ctl').style.display = (m === 'between') ? 'block' : 'none';
  document.getElementById('via_ctl').style.display = (m === 'via') ? 'block' : 'none';
  document.getElementById('topoi_ctl').style.display = (m === 'topoi') ? 'block' : 'none';
  document.getElementById('pois_ctl').style.display = (m === 'pois') ? 'block' : 'none';
  // Composing/naming only make sense in the plain area mode. The saved-area selector,
  // though, stays live for the browse too — "only in the downloaded area" is half of
  // what that mode is for, and it is the one offline mode that costs nothing at all.
  document.getElementById('compose_loops').disabled = (m !== 'area');
  document.getElementById('area').disabled = (m !== 'area' && m !== 'pois');
  const btn = document.getElementById('search');
  btn.textContent = m === 'around' ? 'Search loops near the point'
                  : m === 'between' ? 'Search routes between the points'
                  : m === 'via' ? 'Draw the route through the points'
                  : m === 'topoi' ? 'Draw routes to the nearest'
                  : m === 'pois' ? 'Show the points of interest'
                  : 'Search this map area';
  picks.clearLayers(); aroundPt = fromPt = toPt = toPoiPt = null; viaPts = [];
  markers.clearLayers(); routeLines.clearLayers(); poiMarkers.clearLayers();
  updateHint();
}

document.getElementById('via_undo').addEventListener('click', () => {
  viaPts.pop(); drawPicks(); updateHint();
});

function esc(s){ return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function val(id){ const v = document.getElementById(id).value.trim(); return v === '' ? null : v; }

const FIELDS = ['circular','car_access','chairlift_access','transit_access','ferrata','near_misses',
                'min_gain_m','max_gain_m','min_distance_km','max_distance_km','user_agent'];

function fmtWhen(iso){
  // "2026-08-09T12:34:56+00:00" -> "2026-08-09 12:34 UTC". Sliced, not regexed: this
  // JS lives inside a Python string, where a regex escape would need double-escaping.
  if (!iso) return 'unknown date';
  const s = String(iso).replace('T', ' ').replace('+00:00', '').trim();
  return (s.length >= 16 ? s.slice(0, 16) : s) + ' UTC';
}

function selectArea(name){
  // Pick a downloaded area and frame it, so "search offline" and "look at what I
  // downloaded" are the same gesture.
  const sel = document.getElementById('area');
  sel.value = (sel.value === name) ? '' : name;
  const box = areaBoxes.getLayers().find(l => l._hfName === name);
  if (box && sel.value) map.fitBounds(box.getBounds().pad(0.05));
  renderAreaList(window._hfAreas || []);
  updateHint();
}

function renderAreaList(areas){
  const list = document.getElementById('areas_list');
  const chosen = document.getElementById('area').value;
  document.getElementById('areas_count').textContent =
    areas.length ? '(' + areas.length + ')' : '(none yet)';
  if (!areas.length){
    list.innerHTML = '<p class="muted">Nothing downloaded yet. Draw a box (or use the '
      + 'current view), name it, and press Download.</p>';
    return;
  }
  list.innerHTML = '';
  for (const a of areas){
    const el = document.createElement('div');
    el.className = 'area' + (a.name === chosen ? ' on' : '');
    // An area downloaded before points of interest existed can't answer a "must pass"
    // search, and that must not read as "there are no churches here".
    // Three cases, not one: no POIs at all, POIs but whole KINDS newer than the file,
    // and a file that doesn't record which kinds it holds. Only the first was visible
    // before, and the second is the one a growing registry keeps creating.
    let stale = '';
    if (!a.pois){
      stale = '<div class="warn">no points of interest — re-download to use “Must pass”</div>';
    } else if (a.poi_kinds_missing === null || a.poi_kinds_missing === undefined){
      stale = '<div class="warn">kinds not recorded — an empty result here may mean'
        + ' nobody looked; re-download to be sure</div>';
    } else if (a.poi_kinds_missing.length){
      stale = '<div class="warn">predates ' + a.poi_kinds_missing.length + ' kind(s) ('
        + esc(a.poi_kinds_missing.join(', ')) + ') — re-download to search for them</div>';
    }
    el.innerHTML = '<div class="an">' + esc(a.name) + '</div>'
      + '<div class="ad">' + a.routes + ' routes &middot; ' + a.samples + ' elevation samples'
      + (a.pois ? (' &middot; ' + a.pois + ' places of interest') : '')
      + '</div>'
      + '<div class="ad">' + (a.bytes / 1e6).toFixed(1) + ' MB &middot; ' + fmtWhen(a.created_at) + '</div>'
      + stale;
    el.onclick = () => selectArea(a.name);
    list.appendChild(el);
  }
}

async function loadAreas(selectName){
  // Populate the saved-area selector AND outline every downloaded area on the map, so
  // "what have I already got?" is answerable at a glance instead of by name alone.
  try {
    const areas = await (await fetch('/api/areas')).json();
    window._hfAreas = areas;
    const sel = document.getElementById('area');
    sel.length = 1;  // keep the first "live map" option
    areaBoxes.clearLayers();
    for (const a of areas){
      const o = document.createElement('option');
      o.value = a.name;
      o.textContent = a.name + ' (' + a.routes + ' routes)';
      sel.appendChild(o);
      const b = a.bbox;
      if (b && b.length === 4){
        const rect = L.rectangle([[b[0], b[1]], [b[2], b[3]]], {
          color:'#0b7d63', weight:1, dashArray:'3 4', fillColor:'#0b7d63', fillOpacity:0.04,
        }).addTo(areaBoxes);
        rect._hfName = a.name;
        rect.bindTooltip('downloaded: ' + a.name, { sticky:true });
        rect.on('click', ev => { L.DomEvent.stop(ev); selectArea(a.name); });
      }
    }
    if (selectName) sel.value = selectName;
    renderAreaList(areas);
  } catch (e){ /* best-effort */ }
}

async function loadPois(){
  // The destination kinds come from the server's ONE registry, so the list can never
  // offer something the engine would reject.
  try {
    const kinds = await (await fetch('/api/pois')).json();
    // The SAME registry feeds both lists: what a route may be filtered by ("must pass")
    // and what it may be drawn to ("walk to"). One fetch, so the two can never disagree.
    // Three lists, one registry: what a route may be FILTERED by ("must pass"), what it
    // may be DRAWN to ("walk to"), and what may simply be LISTED ("show"). One fetch, so
    // none of the three can offer a kind the other two don't know.
    for (const id of ['poi', 'to_poi', 'show_poi']){
      const sel = document.getElementById(id);
      for (const k of kinds){
        const o = document.createElement('option');
        o.value = k.kind; o.textContent = k.label;
        sel.appendChild(o);
      }
    }
  } catch (e){ /* best-effort */ }
}

function selectedPois(){
  return Array.from(document.getElementById('poi').selectedOptions).map(o => o.value);
}

function selectedToPois(){
  return Array.from(document.getElementById('to_poi').selectedOptions).map(o => o.value);
}

function selectedShowPois(){
  // Nothing selected means EVERY kind (the server expands it) — a browse with no
  // selection is "what's here?", not "nothing".
  return Array.from(document.getElementById('show_poi').selectedOptions).map(o => o.value);
}

async function showPois(area, status){
  // The browse mode: list the objects, pin them, and make them downloadable — no routes,
  // no elevation, no quota. Its own request/render path because the payload is objects,
  // not hikes; sharing render() would mean faking a hike around each pin.
  const params = new URLSearchParams();
  params.set('pois', 'true');                    // also what makes the GPX/GeoJSON export
  if (area) params.set('area', area);            // offline: the downloaded rectangle
  else boundsParams(params);                     // live: your drawn box, else the view
  for (const k of selectedShowPois()) params.append('show_poi', k);
  const ua = val('ua'); if (ua !== null) params.set('user_agent', ua);
  // Distance/gain/shape filters describe walks, and there is no walk here — sending them
  // would put dead weight in the URL the download replays.
  lastParams = params.toString();

  const results = document.getElementById('results');
  status.textContent = area ? ('Reading “' + area + '” offline…') : 'Looking…';
  results.innerHTML = '';
  markers.clearLayers(); routeLines.clearLayers(); poiMarkers.clearLayers();
  try {
    const resp = await fetch('/api/poi-list?' + params.toString());
    const data = await resp.json();
    if (!resp.ok || data.error){ status.textContent = 'Error: ' + (data.error || resp.status); return; }
    const list = data.pois || [];
    renderPois(list);
    const gap = data.area_gap || {state: 'ok', kinds: [], message: ''};
    if (!list.length && data.stale_area){
      // "This area has no ruins" and "this file was saved before the feature existed"
      // are different answers with different fixes — never let the second read as the first.
      status.textContent = 'This downloaded area carries no points of interest — it was '
        + 'saved before the feature existed. Download it again to browse and export them offline.';
    } else if (gap.state === 'missing'){
      // Said whether or not the list is empty: a listing of ruins from a file that never
      // looked for trees is not the answer to "ruins and trees", and printed bare it
      // reads as one.
      status.textContent = gap.message
        + (list.length ? ' Showing what it does carry: ' + data.summary : '');
    } else if (!list.length && gap.state === 'unknown'){
      status.textContent = 'This downloaded area does not record which kinds it was saved '
        + 'with, so an empty result cannot be told apart from a kind nobody looked for. '
        + 'Download it again if you expected something here.';
    } else if (!list.length){
      status.textContent = 'Nothing of that kind is mapped here — pick other kinds (or none, '
        + 'for all of them), or look at a wider area. (A miss means nothing of that kind is '
        + 'mapped in OSM here, not that nothing is there.)';
    } else {
      status.textContent = data.summary + (area ? ' [offline]' : '');
    }
  } catch (e){ status.textContent = 'Request failed: ' + e; }
}

function renderPois(places){
  // One pin and one list entry per object. Clicking either frames it, so a long list
  // stays navigable on the map.
  const results = document.getElementById('results');
  places.forEach(p => {
    const title = p.name || p.label;
    const pin = L.circleMarker([p.lat, p.lon], { radius:7, color:'#0b7d63', weight:2,
        fillColor:'#8ce0c8', fillOpacity:0.95 }).addTo(poiMarkers)
      .bindPopup('<b>' + esc(title) + '</b><br>' + esc(p.label)
                 + '<br>' + p.lat.toFixed(5) + ', ' + p.lon.toFixed(5));
    const el = document.createElement('div');
    el.className = 'hike';
    el.innerHTML = '<div class="name">' + esc(title) + '</div>'
      + '<div class="meta">' + esc(p.label) + '</div>'
      + '<div class="muted">' + p.lat.toFixed(5) + ', ' + p.lon.toFixed(5) + '</div>';
    el.onclick = () => { map.setView([p.lat, p.lon], 16); pin.openPopup(); };
    results.appendChild(el);
  });
}

async function search(){
  const mode = modeName();
  const area = document.getElementById('area').value;
  const status = document.getElementById('status');
  // Cleared FIRST, ahead of the mode guards below and of the browse dispatch: several
  // of those return early ("drop your point first"), and a caveat left standing would
  // describe a search nobody is looking at.
  clearNotices();
  if (mode === 'pois'){ await showPois(area, status); return; }
  const params = new URLSearchParams();
  if (mode === 'around'){
    if (!aroundPt){ status.textContent = 'Click the map to drop your point first.'; return; }
    params.set('around_lat', aroundPt.lat); params.set('around_lon', aroundPt.lng);
    const r = val('around_radius_m'); if (r !== null) params.set('around_radius_m', r);
  } else if (mode === 'between'){
    if (!fromPt || !toPt){ status.textContent = 'Drop both a start and a finish on the map first.'; return; }
    params.set('from_lat', fromPt.lat); params.set('from_lon', fromPt.lng);
    params.set('to_lat', toPt.lat); params.set('to_lon', toPt.lng);
    const k = val('routes_k'); if (k !== null) params.set('routes_k', k);
  } else if (mode === 'via'){
    if (viaPts.length < 2){ status.textContent = 'Drop at least two waypoints on the map first.'; return; }
    for (const p of viaPts) params.append('via', p.lat + ',' + p.lng);  // repeated, order preserved
    if (document.getElementById('via_loop').checked) params.set('via_loop', 'true');
  } else if (mode === 'topoi'){
    if (!toPoiPt){ status.textContent = 'Click the map to drop your start first.'; return; }
    const kinds = selectedToPois();
    if (!kinds.length){ status.textContent = 'Pick what to walk to (a church, a ruin, a peak…).'; return; }
    params.set('to_poi_lat', toPoiPt.lat); params.set('to_poi_lon', toPoiPt.lng);
    for (const k of kinds) params.append('to_poi', k);
    const n = val('to_poi_n'); if (n !== null) params.set('to_poi_n', n);
    const rr = val('to_poi_radius_m'); if (rr !== null) params.set('to_poi_radius_m', rr);
  } else if (area){
    params.set('area', area);                 // offline: bbox comes from the snapshot
  } else {
    boundsParams(params);                     // your drawn box, else the whole view
    // Loop composition is a live-map-only mode (it builds a graph from fetched OSM).
    if (document.getElementById('compose_loops').checked) params.set('compose_loops', 'true');
  }
  // The destination filter applies to EVERY mode — an area search, a composed loop, a
  // route between two points — because "does this go past a ruin?" is a property of the
  // route, not of how the route was found.
  const pois = selectedPois();
  for (const k of pois) params.append('poi', k);
  if (pois.length){
    const pr = val('poi_radius_m'); if (pr !== null) params.set('poi_radius_m', pr);
  }
  // Reverse-geocode naming applies only to the plain live-area search (unnamed relations).
  if (mode === 'area' && !area && document.getElementById('name_places').checked) params.set('name_places', 'true');
  for (const f of FIELDS){
    // `circular` is a shape filter only meaningful for the area search — a loop is always
    // circular and a between-route never is, so sending it there would filter everything out.
    if (f === 'circular' && mode !== 'area') continue;
    const id = (f === 'user_agent') ? 'ua' : f; const v = val(id); if (v !== null) params.set(f, v);
  }
  // Remember exactly what we searched, so the GPX/GeoJSON download reproduces THIS
  // result set (the same points/bbox/filters) rather than the current map view.
  lastParams = params.toString();

  const results = document.getElementById('results');
  status.textContent = area ? ('Searching “' + area + '” offline…') : 'Searching…';
  results.innerHTML = '';
  markers.clearLayers(); routeLines.clearLayers(); poiMarkers.clearLayers();
  try {
    const resp = await fetch('/api/hikes?' + params.toString());
    const data = await resp.json();
    if (!resp.ok || data.error){ status.textContent = 'Error: ' + (data.error || resp.status); return; }
    // An object, not a bare array: the routes, plus what the source could not answer.
    const hikes = data.hikes || [];
    const notices = data.notices || [];
    render(hikes);
    showNotices(notices);
    const near = hikes.filter(h => h.near_miss).length;
    const composing = mode === 'area' && !area && document.getElementById('compose_loops').checked;
    const viaLoop = mode === 'via' && document.getElementById('via_loop').checked;
    const noun = (mode === 'between' || mode === 'topoi') ? ' route(s)'
               : (mode === 'via') ? (viaLoop ? ' circular route' : ' route')
               : (mode === 'around' || composing) ? ' loop(s)' : ' match(es)';
    // Outranks every sentence below it, which is why it is checked first rather than
    // added beside them: each of those blames a filter for excluding something, and with
    // no route relations in the area there was nothing to exclude. Saying both would put
    // "nothing is mapped here" and "widen the map" on screen at once.
    const noRoutes = notices.find(n => n.kind === 'no_routes');
    if (noRoutes){
      status.textContent = noRoutes.message;
    } else if (hikes.length === 0 && mode === 'topoi'){
      // Destination-shaped, and checked BEFORE the "must pass" wording below: nothing was
      // filtered out of an area here, a route to an object could not be drawn. The three
      // causes need three different fixes, so name all three rather than guess.
      status.textContent = 'No route could be drawn to a ' + selectedToPois().join(' or a ')
        + ' — either nothing of that kind is mapped within “look within”, the ones found sit '
        + 'off the trail network, or every route to them runs past the max distance. '
        + '(A miss means nothing of that kind is mapped in OSM near your start.)';
    } else if (hikes.length === 0 && pois.length){
      // With a destination filter on, the usual culprit is the radius or the kind —
      // not the distance/gain band — so point at the right lever.
      status.textContent = 'Nothing here passes a ' + pois.join(' or a ')
        + ' — widen “how close counts”, pick another kind, or search a wider area. '
        + '(A miss means nothing of that kind is mapped in OSM near a route.)';
    } else if (hikes.length === 0){
      status.textContent = mode === 'around'
          ? 'No loops pass within the radius of your point — widen the radius or the min/max distance.'
        : mode === 'between'
          ? 'No routes between your two points — move them onto/closer to marked trails, or raise the max distance.'
        : mode === 'via'
          ? 'No route through your waypoints — move them onto/closer to marked trails, or check they are on one connected network.'
        : composing
          ? 'No loops could be composed here — widen the map or the min/max distance.'
          : 'No matches — widen the map or relax the filters.';
    } else {
      status.textContent = (hikes.length - near) + noun
        + (near ? (' + ' + near + ' near miss(es)') : '') + (area ? ' [offline]' : '');
    }
    if (!area) showQuota();
  } catch (e){ status.textContent = 'Request failed: ' + e; }
}

function clearNotices(){
  document.getElementById('notices').innerHTML = '';
}

function showNotices(notices){
  // Everything EXCEPT `no_routes`, which the caller has already put in the status line
  // in place of the "widen the map" advice it contradicts. What lands here is the class
  // of caveat that must be visible alongside a perfectly good list of routes — today the
  // ferrata gap, where an unexplained result reads as a safety claim nobody made.
  const box = document.getElementById('notices');
  box.innerHTML = '';
  (notices || []).filter(n => n.kind !== 'no_routes').forEach(n => {
    const el = document.createElement('div');
    el.className = 'notice';
    el.textContent = n.message;      // textContent, not innerHTML: server prose, unparsed
    box.appendChild(el);
  });
}

async function downloadArea(){
  const name = (document.getElementById('area_name').value || '').trim();
  if (!name){ document.getElementById('status').textContent = 'Enter a name for this view first.'; return; }
  // The SAME selection the search uses — your drawn box if there is one.
  const params = new URLSearchParams({ name });
  boundsParams(params);
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
      + data.samples + ' elevation samples, ' + (data.pois || 0) + ' places of interest'
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
    // Only a positive result is shown, and it names the kind — same rule as format.py:
    // false and "never recorded" (null) are different, and a flag can't say which.
    if (h.transit_access) flags.push('transit:' + esc(h.transit_label || h.transit_type));
    // Cabled climbing, gated on nothing but presence — the one flag that must survive
    // describing a small minority of a route's metres (see ferrata.py). `label` is built
    // server-side so the terminal, the MCP text and this list can't word it differently.
    if (h.ferrata && h.ferrata.present) flags.push(esc(h.ferrata.label || 'ferrata'));
    const gain = (h.gain_m != null) ? ('+' + h.gain_m + ' m / -' + h.loss_m + ' m') : 'gain n/a';
    const note = (h.near_miss && h.notes && h.notes.length)
      ? '<div class="note">near miss: ' + esc(h.notes.join('; ')) + '</div>' : '';
    // What the route actually reaches: listed with the measured distance, and pinned on
    // the map so "goes past a ruin" is something you can see rather than take on trust.
    let passes = '';
    if (h.pois && h.pois.length){
      const parts = h.pois.map(p => esc(p.label + (p.name ? ' “' + p.name + '”' : ''))
                                    + ' (' + Math.round(p.distance_m) + ' m)');
      passes = '<div class="passes">passes ' + parts.join('; ') + '</div>';
      for (const p of h.pois){
        L.circleMarker([p.lat, p.lon], { radius:6, color:'#0b7d63', weight:2,
            fillColor:'#8ce0c8', fillOpacity:0.95 }).addTo(poiMarkers)
          .bindPopup('<b>' + esc(p.name || p.label) + '</b><br>' + esc(p.label)
                     + '<br>' + Math.round(p.distance_m) + ' m from ' + esc(dispName));
      }
    }
    // What the route was drawn TO, as opposed to what it passes. Pinned in its own colour
    // and worded "ends N m from" — the route stops at the nearest point on a trail, which
    // is not the object, and the map must not imply you can walk onto its doorstep.
    let goesTo = '';
    if (h.destination){
      const d = h.destination;
      const dname = d.label + (d.name ? ' “' + d.name + '”' : '');
      goesTo = '<div class="passes">ends ' + Math.round(d.distance_m) + ' m from the '
             + esc(dname) + '</div>';
      L.circleMarker([d.lat, d.lon], { radius:8, color:'#c5221f', weight:3,
          fillColor:'#ffd7d5', fillOpacity:0.95 }).addTo(poiMarkers)
        .bindPopup('<b>' + esc(d.name || d.label) + '</b><br>' + esc(d.label)
                   + '<br>route ends ' + Math.round(d.distance_m) + ' m away');
    }
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
      + goesTo
      + passes
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
  if (!lastParams){
    // Match the verb of the button that produces the list — "search" is wrong in the
    // browse mode, where the button says Show.
    status.textContent = (modeName() === 'pois' ? 'Show the points of interest first'
                                                : 'Search first')
                       + ', then download GPX/GeoJSON.';
    return;
  }
  window.location = '/api/' + fmt + '?' + lastParams;
}
document.getElementById('search').onclick = search;
document.getElementById('mode').onchange = updateMode;
updateMode();
document.getElementById('download').onclick = downloadArea;
document.getElementById('dl_gpx').onclick = () => download('gpx');
document.getElementById('dl_geojson').onclick = () => download('geojson');
document.getElementById('area').onchange = () => {
  renderAreaList(window._hfAreas || []);
  updateHint();   // the browse mode's hint says live-view vs downloaded-area
};
updateSel();
loadAreas();
loadPois();
</script>
</body>
</html>
"""


def _fetch_error(e: Exception) -> tuple[int, dict]:
    """A 502 error body for a failed live fetch, with the Overpass 406 hint appended."""
    msg = str(e)
    if "406" in msg:
        msg += (
            " — fill in the Contact field (the public Overpass server rejects "
            "the default User-Agent)."
        )
    return (502, {"error": f"failed to fetch OSM data: {msg}"})


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


def _poi_kinds(qs: dict, key: str = "poi") -> tuple[str, ...]:
    """The requested POI kinds, from repeated ``key=`` and/or comma-separated values.

    Serves both ``poi`` (the "must pass" filter) and ``to_poi`` (the destination to draw a
    route to) — same spelling rules, same validation, one implementation so the two can't
    drift. Raises ``ValueError`` (surfaced as a 400) on an unknown kind, so a stale bookmark
    or a hand-typed query fails visibly instead of quietly matching nothing.
    """
    raw: list[str] = []
    for item in qs.get(key, []):
        raw.extend(part for part in str(item).split(",") if part.strip())
    return normalise_kinds(raw)


def _live_notices(diagnostics: dict) -> list[dict]:
    """What a LIVE fetch could not answer, worded for the browser.

    The live counterpart of ``_area_notices``, and shorter for a reason that is worth
    stating rather than leaving as an absence: there is exactly one kind here, never the
    ferrata gap. A live fetch always parses ``ferrata_routes``/``ferrata_ways`` and the
    member-way tags, and the ferrata clause changed the query TEXT — the Overpass cache
    key — so a pre-feature response cannot be served under one either. On this path
    ``ferrata_gap_message`` is provably ``None``, and a seam that can never fire reads as
    one that might.

    Used by every live mode, the point-based ones included. They derive their own bbox
    from the point(s) you clicked, so "OSM maps no hiking route relations in this area"
    is exactly as true of that derived box as of a drawn one — and exactly as invisible
    without this, since each of those modes otherwise answers an empty search by blaming
    a radius, a snap distance or a filter. The page puts a ``no_routes`` message in the
    status line INSTEAD of that advice (see ``showNotices``), so nothing has to change
    per mode for it to land right.
    """
    if diagnostics.get("no_routes"):
        return [{"kind": "no_routes", "message": no_routes_message()}]
    return []


def _area_notices(area, criteria: Criteria) -> list[dict]:
    """What a SAVED area cannot answer about this search, worded for the browser.

    Each entry is ``{"kind", "message"}``. The kind is what the page keys its rendering
    off, and the two kinds are rendered differently on purpose:

    * ``no_routes`` — nothing here is mapped as a hiking route. It **replaces** the page's
      empty-result sentence rather than joining it: "widen the map or relax the filters"
      is precisely the advice ``no_routes_message`` exists to delete, and showing both
      would answer the same question twice, contradictorily. The CLI and the MCP server
      both word this as outranking every other empty message.
    * ``ferrata_gap`` — cable cannot be read from this file (or was never fetched into
      it). Shown whenever the ferrata filter is active and the gap is real, **including
      when routes did come back**: this project never gates a caveat on an empty result
      (see ``search.snapshot_poi_gap``), and here the reason is sharper still — a
      ``ferrata=false`` search of an unreadable area returns nothing, and nothing is
      exactly what reads as "there is nothing cabled here".

    The messages come from the same ``search`` functions the CLI and the MCP server use,
    so the three frontends cannot word one file's shortfall three ways. What the web adds
    is a *channel*: ``search_snapshot`` logs these, and a log line reaches a terminal.
    """
    notices: list[dict] = []
    if area_has_no_routes(area):
        notices.append({"kind": "no_routes", "message": no_routes_message()})
    if criteria.ferrata is not None:
        # ONE function picks between the two ferrata sentences, and the order it picks
        # them in is what keeps each true (see search.ferrata_gap_message).
        gap = ferrata_gap_message(area, finding=criteria.ferrata is True)
        if gap is not None:
            notices.append({"kind": "ferrata_gap", "message": gap})
    return notices


def _cfg_for(qs: dict):
    """Env config with the per-search knobs the form can override applied on top."""
    cfg = _config.load()
    radius = _num(qs, "poi_radius_m")
    if radius is not None:
        cfg.poi_radius_m = radius
    return cfg


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
        if parsed.path == "/api/poi-list":
            # The objects in an area. Distinct from /api/pois below, which serves the
            # KIND registry — the menu, not the things.
            self._poi_list(parse_qs(parsed.query))
            return
        if parsed.path == "/api/pois":
            # The selectable destination kinds, served from the ONE registry so the UI
            # list can never offer something the engine doesn't know (see poi.py).
            self._json(200, [{"kind": k, "label": lbl} for k, lbl in kind_labels()])
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
        """"What have I already downloaded?" — one entry per named snapshot, with its
        bbox so the map can outline the covered ground, not merely name it.

        Each entry is enriched with ``poi_kinds_missing``: how many registered kinds
        postdate that file (``null`` when it does not record a kind set at all). The diff
        is taken HERE rather than in the browser because ``poi.unrecorded_kinds`` is the
        one place that owns it — JS re-deriving it from ``/api/pois`` would be a second
        implementation of the comparison, free to disagree with the CLI's.
        """
        areas = list_snapshots()
        for a in areas:
            recorded = a.get("poi_kinds")
            behind = unrecorded_kinds(tuple(recorded) if recorded is not None else None)
            a["poi_kinds_missing"] = None if behind is None else list(behind)
        self._json(200, areas)

    def _download(self, qs: dict) -> None:
        name = _str(qs, "name")
        path = snapshot_path(name) if name else None
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
                msg += (
                    " — fill in the Contact field (the public Overpass server rejects "
                    "the default User-Agent)."
                )
            self._json(502, {"error": f"download failed: {msg}"})
            return
        self._json(200, {
            "name": path.stem, "routes": snap.route_count,
            "samples": snap.sample_count, "places": snap.place_count,
            "pois": snap.poi_count,
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

    def _bbox(self, qs: dict):
        """``(bbox, None)`` or ``(None, (400, {...}))`` — the live search rectangle."""
        try:
            return (
                float(qs["south"][0]),
                float(qs["west"][0]),
                float(qs["north"][0]),
                float(qs["east"][0]),
            ), None
        except (KeyError, ValueError):
            return None, (400, {"error": "south/west/north/east are required"})

    def _resolve_pois(self, qs: dict):
        """List an area's points of interest — the browse mode, no routes drawn.

        Deliberately NOT a branch inside ``_resolve_hikes``: that function returns hikes
        and, more to the point, *rejects* pairing a saved area with a POI destination
        (routing needs a live graph). Browsing has the opposite requirement — "everything
        in the area I downloaded" is half of what the mode is for — so it gets its own
        resolver rather than an exception carved into the other one's rules.

        Returns ``(places, gap, None)`` or ``(None, _GAP_OK, (status, {...}))``, where
        *gap* is the ``(state, kinds)`` pair from ``search.snapshot_poi_gap`` rendered as
        a dict for JSON. It tells the UI which of the four things is true of the source —
        that it predates points of interest entirely, that it predates the named kinds,
        that it cannot say, or that the answer is about the landscape — so an empty
        listing is never shown as "there is nothing here" when nobody looked. A live
        listing is always ``ok``: the fetch just happened against this build's registry.
        """
        try:
            kinds = _poi_kinds(qs, "show_poi")
        except ValueError as e:
            return None, _GAP_OK, (400, {"error": str(e)})
        area_name = _str(qs, "area")
        if area_name:
            path = snapshot_path(area_name)
            if path is None or not path.is_file():
                return None, _GAP_OK, (404, {"error": f"no saved area named {area_name!r}"})
            try:
                snap = load_snapshot(path)
            except (OSError, ValueError) as e:
                return None, _GAP_OK, (500, {"error": f"could not read area: {e}"})
            state, gap_kinds = snapshot_poi_gap(snap, kinds)
            return (
                list_snapshot_pois(snap, kinds),
                {
                    "state": state,
                    "kinds": list(gap_kinds or ()),
                    # Pre-worded here rather than in JS for the same reason the CLI and the
                    # MCP server share it: one sentence, three frontends.
                    "message": (
                        snapshot_kinds_missing_message(gap_kinds or ())
                        if state == "missing"
                        else ""
                    ),
                },
                None,
            )
        bbox, err = self._bbox(qs)
        if err is not None:
            return None, _GAP_OK, err
        try:
            return list_area_pois(
                bbox, kinds, _cfg_for(qs), user_agent=_str(qs, "user_agent")
            ), _GAP_OK, None
        except Exception as e:  # noqa: BLE001 — surface any fetch/HTTP failure to the UI
            return None, _GAP_OK, _fetch_error(e)

    def _poi_list(self, qs: dict) -> None:
        """``/api/poi-list`` — the objects themselves, as JSON.

        An object rather than the bare array ``/api/hikes`` returns, because this mode has
        two things to say beyond the list: the kind mix (rendered by the SAME
        ``format_poi_summary`` the CLI prints, so the two frontends can't word it
        differently) and whether an empty result came from a snapshot that predates the
        feature.
        """
        places, gap, err = self._resolve_pois(qs)
        if err is not None:
            self._json(*err)
            return
        self._json(200, {
            "pois": [p.to_dict() for p in places],
            "summary": format_poi_summary(places),
            # The original boolean, kept as-is: it means exactly what it always meant
            # ("this file predates points of interest entirely"), and `area_gap` beside it
            # carries the finer cases. Widening `stale_area` to cover a file that merely
            # predates some KINDS would silently change what an existing reader is told.
            "stale_area": gap["state"] == "none",
            "area_gap": gap,
        })

    def _resolve_hikes(self, qs: dict):
        """Run the search a query describes (offline --area or live bbox/compose).

        Shared by ``/api/hikes`` (JSON) and ``/api/gpx`` / ``/api/geojson`` (file
        download) so all three agree on filters, area resolution, and error handling.
        Returns ``(hikes, notices, None)`` on success or
        ``(None, [], (status, {"error": ...}))`` — the same triple shape
        ``_resolve_pois`` returns, and for the same reason: a result on its own is not
        the whole answer when the source could not be asked the question (see
        ``_area_notices``). The download paths simply drop the notices; a GPX file has
        nowhere to put a sentence, and the search that produced it already showed one.
        """
        try:
            poi_kinds = _poi_kinds(qs)
            to_poi_kinds = _poi_kinds(qs, "to_poi")
        except ValueError as e:
            return None, [], (400, {"error": str(e)})
        cfg = _cfg_for(qs)
        criteria = Criteria(
            min_gain_m=_num(qs, "min_gain_m"),
            max_gain_m=_num(qs, "max_gain_m"),
            min_distance_km=_num(qs, "min_distance_km"),
            max_distance_km=_num(qs, "max_distance_km"),
            circular=_tri(qs, "circular"),
            car_access=_tri(qs, "car_access"),
            chairlift_access=_tri(qs, "chairlift_access"),
            transit_access=_tri(qs, "transit_access"),
            poi_kinds=poi_kinds,
            ferrata=_tri(qs, "ferrata"),
        )
        # near_misses tri-state: absent -> "auto", true -> always, false -> never.
        nm = _tri(qs, "near_misses")
        near_miss = "auto" if nm is None else nm
        # Reverse-geocode naming of unnamed routes (opt-in checkbox; off by default).
        name_places = _tri(qs, "name_places")

        area_name = _str(qs, "area")
        if area_name and to_poi_kinds:
            # Routing to an object is a live search (it needs a trail graph and the
            # elevation pass), and a snapshot search is not one. Saying so beats silently
            # dropping the destination and returning a filtered area search that LOOKS
            # like an answer — the CLI rejects the same pair for the same reason.
            return None, [], (
                400,
                {"error": "to_poi draws a live route; it can't be combined with a saved area"},
            )
        if area_name:
            # Offline: search a saved snapshot — no network, no API calls.
            path = snapshot_path(area_name)
            if path is None or not path.is_file():
                return None, [], (404, {"error": f"no saved area named {area_name!r}"})
            try:
                snap = load_snapshot(path)
                hikes = search_snapshot(
                    snap, criteria, cfg=cfg, near_miss=near_miss, name_places=name_places
                )
            except (OSError, ValueError) as e:
                return None, [], (500, {"error": f"could not search snapshot: {e}"})
            # The saved file is the only source that can fall short of the question, so
            # this is where both caveats come from. `search_snapshot` logs them, which
            # reaches a terminal and not a browser — hence the same message, carried.
            return hikes, _area_notices(snap.area, criteria), None

        ua = _str(qs, "user_agent")

        # Circular routes near a picked point (derives its own area from the point).
        around_lat, around_lon = _num(qs, "around_lat"), _num(qs, "around_lon")
        if around_lat is not None and around_lon is not None:
            diagnostics: dict = {}
            try:
                hikes = compose_loops_around(
                    (around_lat, around_lon), criteria, cfg=cfg,
                    radius_m=_num(qs, "around_radius_m"),
                    user_agent=ua, near_miss=near_miss, diagnostics=diagnostics,
                )
            except Exception as e:  # noqa: BLE001 — surface any fetch/HTTP failure to the UI
                return None, [], _fetch_error(e)
            return hikes, _live_notices(diagnostics), None

        # Routes to the nearest church / ruin / peak from a picked point (derives its own
        # area). Distinct from `poi_kinds` above, which only FILTERS whatever routes a
        # search already produced — here the object is what the route is drawn to.
        tp_lat, tp_lon = _num(qs, "to_poi_lat"), _num(qs, "to_poi_lon")
        if to_poi_kinds and tp_lat is not None and tp_lon is not None:
            n = _num(qs, "to_poi_n")
            diagnostics = {}
            try:
                hikes = routes_to_poi(
                    (tp_lat, tp_lon), to_poi_kinds, criteria, cfg=cfg,
                    n=int(n) if n else None,
                    search_radius_m=_num(qs, "to_poi_radius_m"),
                    user_agent=ua, diagnostics=diagnostics,
                )
            except Exception as e:  # noqa: BLE001
                return None, [], _fetch_error(e)
            # `no_routes` outranks this mode's own empty sentence, which names three
            # causes ("nothing of that kind mapped / off the network / past the max
            # distance") and would blame all three for a box with no trails in it. The
            # page already checks the notice first, so the precedence is the area mode's,
            # unchanged, rather than a second rule invented here.
            return hikes, _live_notices(diagnostics), None
        if to_poi_kinds and (tp_lat is None) != (tp_lon is None):
            return None, [], (400, {"error": "to_poi needs both to_poi_lat and to_poi_lon"})

        # N shortest routes between two picked points (derives its own area).
        f_lat, f_lon = _num(qs, "from_lat"), _num(qs, "from_lon")
        t_lat, t_lon = _num(qs, "to_lat"), _num(qs, "to_lon")
        if None not in (f_lat, f_lon, t_lat, t_lon):
            k = _num(qs, "routes_k")
            diagnostics = {}
            try:
                hikes = routes_between(
                    (f_lat, f_lon), (t_lat, t_lon), criteria, cfg=cfg,
                    k=int(k) if k else None, user_agent=ua, diagnostics=diagnostics,
                )
            except Exception as e:  # noqa: BLE001
                return None, [], _fetch_error(e)
            return hikes, _live_notices(diagnostics), None

        # One route linking several picked points ('via'), optionally closed into a
        # non-retracing circular route. Each waypoint arrives as a repeated `via=lat,lon`.
        raw_via = qs.get("via", [])
        if raw_via:
            points = []
            for item in raw_via:
                try:
                    lat_s, lon_s = item.split(",", 1)
                    points.append((float(lat_s), float(lon_s)))
                except (ValueError, AttributeError):
                    return None, [], (400, {"error": f"bad via point {item!r} (want 'lat,lon')"})
            if len(points) < 2:
                return None, [], (400, {"error": "give at least two via points to link"})
            diagnostics = {}
            try:
                hikes = route_via(
                    points, criteria, cfg=cfg,
                    loop=_tri(qs, "via_loop") is True, user_agent=ua,
                    diagnostics=diagnostics,
                )
            except Exception as e:  # noqa: BLE001
                return None, [], _fetch_error(e)
            return hikes, _live_notices(diagnostics), None

        bbox, bbox_err = self._bbox(qs)
        if bbox_err is not None:
            return None, [], bbox_err

        # Loop composition: synthesise loops from connected trails inside the box.
        composing = _tri(qs, "compose_loops")
        search = compose_loops if composing else search_hikes
        # Filled by the search with facts about the FETCH the hikes cannot carry — the
        # same out-parameter the CLI and the MCP server read (see search.search_hikes).
        diagnostics = {}
        kwargs = {"cfg": cfg, "user_agent": ua, "near_miss": near_miss, "diagnostics": diagnostics}
        # Naming applies only to ordinary routes — composed loops already carry their
        # constituent-trail label, never a route/<id> fallback.
        if not composing:
            kwargs["name_places"] = name_places
        try:
            hikes = search(bbox, criteria, **kwargs)
        except Exception as e:  # noqa: BLE001 — surface any fetch/HTTP failure to the UI
            return None, [], _fetch_error(e)
        # No ferrata notice on a live path, and that is not an oversight: a live fetch
        # always parses `ferrata_routes`/`ferrata_ways` and member-way tags, and the
        # ferrata clause changed the query TEXT — the Overpass cache key — so a
        # pre-feature response cannot be served under it either. `ferrata_gap_message`
        # is provably None here, and a seam that can never fire reads as one that might.
        return hikes, _live_notices(diagnostics), None

    def _api(self, qs: dict) -> None:
        hikes, notices, err = self._resolve_hikes(qs)
        if err is not None:
            self._json(*err)
            return
        # An OBJECT, not the bare array this used to return. The array had nowhere to put
        # a sentence about what the SOURCE could not answer, so the web UI was the one
        # frontend that showed an empty list where the CLI and the MCP server explain
        # themselves — and for `ferrata=false` over an unreadable area, an unexplained
        # empty list reads as "no safe routes here", the exact inversion that feature
        # spends its comments preventing. `/api/poi-list` already carries its gap this way.
        # geometry=True so the map can draw the route lines without a second search.
        self._json(200, {
            "hikes": [hike_to_dict(h, geometry=True) for h in hikes],
            "notices": notices,
        })

    def _export(self, qs: dict, fmt: str) -> None:
        """Run the query's search and stream the results as a GPX / GeoJSON download.

        The browse mode branches at the TOP rather than inside ``_resolve_hikes``, which
        keeps the download plumbing identical for both: the page stores the params of the
        last search and replays them here, so whichever mode produced what you are looking
        at, the file matches it. ``pois=true`` is simply part of those stored params.
        """
        if _tri(qs, "pois") is True:
            places, _stale, err = self._resolve_pois(qs)
            if err is not None:
                self._json(*err)
                return
            if fmt == "gpx":
                body, mime, filename = pois_to_gpx(places), GPX_MIME, "pois.gpx"
            else:
                body, mime, filename = (
                    pois_to_geojson(places), GEOJSON_MIME, "pois.geojson",
                )
            self._stream(body, mime, filename)
            return
        hikes, _notices, err = self._resolve_hikes(qs)
        if err is not None:
            self._json(*err)
            return
        if fmt == "gpx":
            body, mime, filename = hikes_to_gpx(hikes), GPX_MIME, "hikes.gpx"
        else:
            body, mime, filename = hikes_to_geojson(hikes), GEOJSON_MIME, "hikes.geojson"
        self._stream(body, mime, filename)

    def _stream(self, body: str, mime: str, filename: str) -> None:
        """Send a generated document as a file download."""
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
