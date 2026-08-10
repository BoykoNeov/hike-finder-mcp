// Drive the web UI's JavaScript outside a browser.
//
// web.py ships a real Leaflet application inside INDEX_HTML — a drawn-box selection
// shared by the download and the search, a downloaded-areas panel, and the
// points-of-interest filter. None of that is reachable from the Python tests, so this
// stubs Leaflet, the DOM, and fetch, runs the page script for real, and asserts on what
// it does. It already caught one ordering bug (a drag's trailing 'click' being taken for
// a point-pick), which is why the setTimeout stub below defers instead of running inline.
//
// Driven by tests/test_web_js.py, which skips when node is unavailable.
const fs = require('fs');

const handlers = {};   // map event handlers, by event name
function mkBounds(a, b) {
  const s = Math.min(a.lat, b.lat), n = Math.max(a.lat, b.lat);
  const w = Math.min(a.lng, b.lng), e = Math.max(a.lng, b.lng);
  return {
    getSouth: () => s, getNorth: () => n, getWest: () => w, getEast: () => e,
    getSouthWest: () => ({ lat: s, lng: w }), getNorthEast: () => ({ lat: n, lng: e }),
    pad: () => mkBounds({ lat: s, lng: w }, { lat: n, lng: e }),
  };
}
const layerGroups = [];
function mkLayerGroup() {
  const g = { _layers: [], clearLayers(){ this._layers = []; }, getLayers(){ return this._layers; },
              addTo(){ return this; } };
  layerGroups.push(g);
  return g;
}
const container = { style: {} };
const map = {
  _bounds: mkBounds({ lat: 50.70, lng: 15.55 }, { lat: 50.80, lng: 15.70 }),
  setView(){ return this; },
  getBounds(){ return this._bounds; },
  getContainer(){ return container; },
  dragging: { _on: true, disable(){ this._on = false; }, enable(){ this._on = true; } },
  on(ev, fn){ (handlers[ev] = handlers[ev] || []).push(fn); },
  fitBounds(){},
  distance(a, b){
    const [alat, alng] = Array.isArray(a) ? a : [a.lat, a.lng];
    const [blat, blng] = Array.isArray(b) ? b : [b.lat, b.lng];
    const dy = (blat - alat) * 111195;
    const dx = (blng - alng) * 111195 * Math.cos(alat * Math.PI / 180);
    return Math.hypot(dx, dy);
  },
};
const circleMarkers = [];
const shapeProto = { addTo(g){ if (g && g._layers) g._layers.push(this); return this; },
                     bindTooltip(){ return this; }, bindPopup(){ return this; },
                     openPopup(){}, on(){ return this; }, remove(){}, setBounds(b){ this._b = b; },
                     getBounds(){ return this._b; } };
const mk = (extra) => Object.assign(Object.create(shapeProto), extra);
global.L = {
  map: () => map,
  tileLayer: () => mk({}),
  layerGroup: () => mkLayerGroup(),
  // Leaflet accepts either a [[s,w],[n,e]] array (the area outlines) or a bounds
  // object (the live drag rectangle).
  rectangle: (b) => mk({ _b: Array.isArray(b)
      ? mkBounds({ lat: b[0][0], lng: b[0][1] }, { lat: b[1][0], lng: b[1][1] }) : b }),
  // Recorded, not just stubbed: "the ruin is pinned where the ruin is" is a claim worth
  // checking, and a plain layer-count would pass on any marker at all.
  circleMarker: (pt) => { circleMarkers.push(pt); return mk({}); },
  marker: () => mk({}),
  polyline: () => mk({}),
  featureGroup: () => mk({ getBounds: () => map._bounds }),
  latLngBounds: (a, b) => mkBounds(a, b),
  DomEvent: { stop(){} },
};

// --- DOM ------------------------------------------------------------------------
const els = {};
function el(id) {
  if (els[id]) return els[id];
  const e = {
    id, value: '', textContent: '', innerHTML: '', checked: false, disabled: false,
    length: 0, style: {}, className: '', selectedOptions: [], _children: [],
    _listeners: {},
    addEventListener(ev, fn){ (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    appendChild(c){ this._children.push(c); this.length = this._children.length; },
    click(){ (this._listeners.click || []).forEach(f => f()); if (this.onclick) this.onclick(); },
  };
  els[id] = e;
  return e;
}
global.document = {
  getElementById: el,
  createElement: () => ({ value: '', textContent: '', innerHTML: '', className: '',
                          style: {}, appendChild(){}, _children: [] }),
};
global.window = {};
const pendingTimers = [];
global.setTimeout = (fn) => { pendingTimers.push(fn); };
const drainTimers = () => { while (pendingTimers.length) pendingTimers.shift()(); };

// --- fetch ----------------------------------------------------------------------
let fetched = [];
const AREAS = [
  { name: 'krkonose', bbox: [50.70, 15.55, 50.80, 15.70], created_at: '2026-08-09T12:07:51+00:00',
    routes: 12, samples: 3198, places: 0, pois: 280, bytes: 225925, poi_kinds_missing: [] },
  { name: 'oldarea',  bbox: [49.90, 13.90, 50.20, 14.20], created_at: '2025-01-02T08:00:00+00:00',
    routes: 4, samples: 900, places: 0, pois: 0, bytes: 40000, poi_kinds_missing: null },
];
// The four things a saved file can say about its POI coverage, fed straight to the card
// renderer rather than added to AREAS — the startup checks above count the areas, and
// padding that list to exercise a renderer would weaken them into "some number of areas".
const CARD_AREAS = [
  // Records the current registry: nothing to warn about, and a warning here would be the
  // kind that never turns off.
  { ...AREAS[0] },
  // Full of objects, but two kinds postdate it.
  { name: 'oldkinds', bbox: [50.50, 15.09, 50.62, 15.28], created_at: '2026-02-01T08:00:00+00:00',
    routes: 9, samples: 2100, places: 0, pois: 140, bytes: 120000,
    poi_kinds_missing: ['tree', 'mill'] },
  // Full of objects and records NO kind set — it cannot say which questions it was asked.
  { name: 'unrecorded', bbox: [50.10, 14.10, 50.20, 14.20], created_at: '2025-06-01T08:00:00+00:00',
    routes: 6, samples: 1500, places: 0, pois: 90, bytes: 90000, poi_kinds_missing: null },
  // No objects at all: predates points of interest outright.
  { ...AREAS[1] },
];
const POI_KINDS = [{ kind: 'church', label: 'churches & chapels' },
                   { kind: 'ruins', label: 'ruins' },
                   { kind: 'castle', label: 'castles & forts' }];
// One canned result for the "route to the nearest object" mode, so the destination
// rendering (the "ends N m from" wording and its map pin) is exercised, not just the
// query building. Every other search still answers with an empty list.
const TO_POI_HIKES = [
  { osm_id: null, name: 'Route to ruin “Rotštejn”', ref: null,
    unnamed: false, place_name: null, distance_km: 4.2, gain_m: 180, loss_m: 95,
    circular: false, car_access: false, chairlift_access: false, lift_type: null,
    start: { lat: 50.73, lon: 15.60 }, near_miss: false, notes: [],
    composed: true, composed_of: ['0402'], pois: [],
    destination: { kind: 'ruins', label: 'ruin', name: 'Rotštejn',
                   lat: 50.75, lon: 15.61, distance_m: 85.0 },
    geometry: [[[50.73, 15.60], [50.75, 15.61]]] },
];
// The browse mode's payload: objects, not hikes — an OBJECT rather than a bare array,
// because it also carries the kind mix and the "this snapshot predates POIs" signal.
// `oldarea` answers as such a snapshot, so the two empty results can be told apart.
const POI_LIST = {
  pois: [
    { kind: 'church', label: 'church', name: 'Sv. Petr', lat: 50.7211, lon: 15.5902 },
    { kind: 'ruins', label: 'ruin', name: null, lat: 50.7301, lon: 15.6001 },
  ],
  summary: '2 objects: 1 church, 1 ruin',
  stale_area: false,
  area_gap: { state: 'ok', kinds: [], message: '' },
};
const POI_LIST_STALE = {
  pois: [], summary: 'no points of interest', stale_area: true,
  area_gap: { state: 'none', kinds: [], message: '' },
};
// The finer gap, in its two shapes. `stale_area` is FALSE in both: these files are full
// of objects, they simply never looked for the kind being asked about. The empty one is
// the obvious case; the non-empty one is the dangerous case, where half an answer comes
// back and would read as the whole one.
const GAP_MISSING = {
  state: 'missing', kinds: ['tree'],
  message: 'This downloaded area predates the kind tree — it was never sorted into it, '
    + 'so it can only come back empty. That is a fact about the file, not the landscape: '
    + 're-download the area to ask it about this kind.',
};
const POI_LIST_MISSING_EMPTY = {
  pois: [], summary: 'no points of interest', stale_area: false, area_gap: GAP_MISSING,
};
const POI_LIST_MISSING_PARTIAL = {
  pois: [{ kind: 'ruins', label: 'ruin', name: 'Nístějka', lat: 50.7301, lon: 15.6001 }],
  summary: '1 object: 1 ruin', stale_area: false, area_gap: GAP_MISSING,
};
// One ordinary route, for the cases that are about what is said ALONGSIDE a result.
const AREA_HIKES = [
  { osm_id: 7, name: 'WebNorth', ref: null, unnamed: false, place_name: null,
    distance_km: 5.1, gain_m: 120, loss_m: 120, circular: false, car_access: false,
    chairlift_access: false, lift_type: null, start: { lat: 50.72, lon: 15.58 },
    near_miss: false, notes: [], composed: false, composed_of: [], pois: [],
    destination: null, geometry: [[[50.72, 15.58], [50.74, 15.62]]] },
];
// The two things a search can carry beyond its routes (see web.py's `_area_notices`).
// They are rendered DIFFERENTLY, which is the whole reason each carries a `kind`:
// `no_routes` replaces the status line it contradicts, `ferrata_gap` gets its own box
// and survives a non-empty result.
const FERRATA_GAP = { kind: 'ferrata_gap', message:
  'ferrata: this area’s routes carry no member-way tags, so cabled sections cannot '
  + 'be detected on them at all — this is NOT a report that the routes are free of cable.' };
const NO_ROUTES = { kind: 'no_routes', message:
  'No hiking route relations are mapped in that area — this is about the map data, '
  + 'not your filters.' };
function hikesReply(url) {
  // `nofer` answers with routes AND the gap: the browser's rule is that the caveat is
  // shown regardless of what came back, so the case worth driving is the one where a
  // result exists to hide behind.
  if (/area=nofer/.test(url)) return { hikes: AREA_HIKES, notices: [FERRATA_GAP] };
  if (/area=norelations/.test(url)) return { hikes: [], notices: [NO_ROUTES] };
  if (/[?&]to_poi=/.test(url)) return { hikes: TO_POI_HIKES, notices: [] };
  return { hikes: [], notices: [] };
}
global.fetch = async (url) => {
  fetched.push(url);
  const poiList = /area=oldarea/.test(url) ? POI_LIST_STALE
                : !/area=oldkinds/.test(url) ? POI_LIST
                : /show_poi=ruins/.test(url) ? POI_LIST_MISSING_PARTIAL
                : POI_LIST_MISSING_EMPTY;
  const body = url.startsWith('/api/areas') ? AREAS
             : url.startsWith('/api/poi-list') ? poiList
             : url.startsWith('/api/pois') ? POI_KINDS
             : url.startsWith('/api/quota') ? { enabled: false }
             : url.startsWith('/api/hikes') ? hikesReply(url)
             : [];
  return { ok: true, status: 200, json: async () => body };
};

// --- run the page ---------------------------------------------------------------
eval(fs.readFileSync(process.argv[2], 'utf8'));

// ================================================================= assertions
const fail = [];
const ok = [];
const lastHikes = () => fetched.filter(u => u.startsWith('/api/hikes')).pop();
function check(name, cond, detail) {
  (cond ? ok : fail).push(name + (cond ? '' : '  <-- ' + detail));
}
const fire = (ev, latlng) => (handlers[ev] || []).forEach(f => f({ latlng }));

(async () => {
  await new Promise(r => setImmediate(r));   // let loadAreas()/loadPois() settle

  // 1. Areas were fetched, outlined on the map, and listed.
  check('areas fetched', fetched.some(u => u.startsWith('/api/areas')), fetched);
  const boxes = layerGroups.find(g => g._layers.length === 2);
  check('one rectangle drawn per downloaded area', !!boxes, layerGroups.map(g => g._layers.length));
  check('rectangles are tagged with their area name',
        boxes && boxes._layers.map(l => l._hfName).join(',') === 'krkonose,oldarea',
        boxes && boxes._layers.map(l => l._hfName));
  check('area count shown', el('areas_count').textContent === '(2)', el('areas_count').textContent);
  check('pre-POI area flagged for re-download',
        el('areas_list').innerHTML === '' , 'list built via appendChild');
  check('area <select> got both names', el('area').length === 2, el('area').length);

  // 2. POI kinds populated from the server registry.
  check('poi kinds fetched', fetched.some(u => u.startsWith('/api/pois')), fetched);
  check('poi select populated', el('poi')._children.length === 3, el('poi')._children.length);

  // 3. With nothing drawn, the search uses the map view.
  el('near_misses').value = 'false';
  await search();
  let url = lastHikes();
  check('undrawn search uses the map view', url.includes('south=50.7') && url.includes('east=15.7'), url);
  check('sel note explains the default', /whole map view/.test(el('sel_note').textContent),
        el('sel_note').textContent);

  // 4. Draw a box: arm, drag, release.
  el('draw').click();
  check('dragging disabled while drawing', map.dragging._on === false, map.dragging._on);
  fire('mousedown', { lat: 50.72, lng: 15.58 });
  fire('mousemove', { lat: 50.75, lng: 15.63 });
  fire('mouseup',   { lat: 50.75, lng: 15.63 });
  check('dragging re-enabled after drawing', map.dragging._on === true, map.dragging._on);
  check('sel note reports the drawn size', /drawn box/.test(el('sel_note').innerHTML),
        el('sel_note').innerHTML);

  // 5. The SAME box is used by the search and by the download.
  await search();
  url = lastHikes();
  check('drawn box drives the search',
        url.includes('south=50.72') && url.includes('north=50.75') && url.includes('east=15.63'), url);
  el('area_name').value = 'newarea';
  await downloadArea();
  const dl = fetched.filter(u => u.startsWith('/api/download')).pop();
  check('drawn box drives the download too',
        dl && dl.includes('south=50.72') && dl.includes('north=50.75'), dl);

  // 6. A click with no drag cancels rather than arming a metre-wide area.
  el('draw').click();
  fire('mousedown', { lat: 50.73, lng: 15.60 });
  fire('mouseup',   { lat: 50.73, lng: 15.60 });
  await search();
  url = lastHikes();
  check('a no-drag click cancels back to the map view', url.includes('south=50.7&'), url);

  drainTimers();   // a new gesture starts a new task

  // 7. POI selection reaches the query, repeated, with the radius.
  el('poi').selectedOptions = [{ value: 'ruins' }, { value: 'castle' }];
  el('poi_radius_m').value = '600';
  await search();
  url = lastHikes();
  check('poi kinds sent repeated', /poi=ruins/.test(url) && /poi=castle/.test(url), url);
  check('poi radius sent', /poi_radius_m=600/.test(url), url);

  drainTimers();

  // 8. The POI filter applies in a point-based mode too. The picked points live in the
  //    page's own scope, so they are set the way a user sets them: by clicking the map.
  el('mode').value = 'between';
  updateMode();
  fire('click', { lat: 50.72, lng: 15.58 });
  fire('click', { lat: 50.74, lng: 15.62 });
  await search();
  url = lastHikes();
  check('two map clicks set start + finish',
        url.includes('from_lat=50.72') && url.includes('to_lat=50.74'), url);
  check('poi filter applies to the between mode too', /poi=ruins/.test(url), url);

  // 9. Arming the draw tool must not double as a point pick.
  el('mode').value = 'around';
  updateMode();
  check('around mode starts unpicked',
        /Click the map to drop your point/.test(el('status').textContent),
        el('status').textContent);
  el('draw').click();
  fire('click', { lat: 50.71, lng: 15.57 });
  check('a click while the draw tool is armed does not drop a point',
        !/Point set/.test(el('status').textContent), el('status').textContent);
  // Releasing the drag must not drop one either (mouseup also fires 'click').
  fire('mousedown', { lat: 50.71, lng: 15.57 });
  fire('mousemove', { lat: 50.76, lng: 15.65 });
  fire('mouseup',   { lat: 50.76, lng: 15.65 });
  fire('click',     { lat: 50.76, lng: 15.65 });
  check('the drag release does not drop a point either',
        !/Point set/.test(el('status').textContent), el('status').textContent);
  // ...and the suppression is spent: the NEXT genuine click must still register.
  drainTimers();
  fire('click', { lat: 50.73, lng: 15.61 });
  check('the click after the drag still registers a point',
        /Point set/.test(el('status').textContent), el('status').textContent);
  // A drag with no trailing click must not leave the flag armed for a later click.
  el('draw').click();
  fire('mousedown', { lat: 50.71, lng: 15.57 });
  fire('mousemove', { lat: 50.77, lng: 15.66 });
  fire('mouseup',   { lat: 50.77, lng: 15.66 });
  drainTimers();
  el('status').textContent = '';
  fire('click', { lat: 50.72, lng: 15.59 });
  check('no stale suppression after a click-less drag',
        /Point set/.test(el('status').textContent), el('status').textContent);

  // 10. The "route to the nearest church / ruin / peak" mode: its own start point, its
  //     own kind picker, and a destination that must read as "ends N m from", never as
  //     an arrival.
  el('mode').value = 'topoi';
  updateMode();
  check('to-poi mode shows its own controls',
        el('topoi_ctl').style.display === 'block', el('topoi_ctl').style.display);
  check('to-poi mode starts unpicked',
        /Click the map to drop your start/.test(el('status').textContent),
        el('status').textContent);
  check('walk-to list populated from the same registry',
        el('to_poi')._children.length === 3, el('to_poi')._children.length);

  fire('click', { lat: 50.73, lng: 15.60 });
  check('a start with no kind chosen asks for the kind, not for the start',
        /Now pick what to walk to/.test(el('status').textContent), el('status').textContent);
  const before = fetched.length;
  await search();
  check('searching with no kind chosen sends no request', fetched.length === before,
        fetched.slice(before));
  check('and says which half is missing',
        /Pick what to walk to/.test(el('status').textContent), el('status').textContent);

  el('to_poi').selectedOptions = [{ value: 'ruins' }, { value: 'castle' }];
  el('to_poi_n').value = '2';
  el('to_poi_radius_m').value = '4500';
  await search();
  url = lastHikes();
  check('to-poi start sent', url.includes('to_poi_lat=50.73') && url.includes('to_poi_lon=15.6'), url);
  check('to-poi kinds sent repeated', /to_poi=ruins/.test(url) && /to_poi=castle/.test(url), url);
  check('to-poi count + radius sent',
        /to_poi_n=2/.test(url) && /to_poi_radius_m=4500/.test(url), url);
  check('the must-pass filter stays a separate parameter', /[?&]poi=ruins/.test(url), url);
  check('no bbox is sent — the mode derives its own area', !/south=/.test(url), url);

  const card = el('results')._children[el('results')._children.length - 1];
  check('the destination is rendered as a gap, not an arrival',
        /ends 85 m from the ruin/.test(card.innerHTML) && !/arrives/.test(card.innerHTML),
        card.innerHTML);
  check('the destination is pinned where the object actually is',
        circleMarkers.some(p => Array.isArray(p) && p[0] === 50.75 && p[1] === 15.61),
        circleMarkers);
  check('the result line counts routes, not matches',
        /route\(s\)/.test(el('status').textContent), el('status').textContent);

  // Switching away clears the picked start, so it can't leak into the next mode.
  el('mode').value = 'around';
  updateMode();
  el('mode').value = 'topoi';
  updateMode();
  check('changing mode clears the picked start',
        /Click the map to drop your start/.test(el('status').textContent),
        el('status').textContent);

  // 11. The browse mode: list the objects themselves, with no route drawn to any of them.
  const lastPoiList = () => fetched.filter(u => u.startsWith('/api/poi-list')).pop();
  el('mode').value = 'pois';
  updateMode();
  check('browse mode shows its own controls',
        el('pois_ctl').style.display === 'block', el('pois_ctl').style.display);
  check('browse list populated from the same registry',
        el('show_poi')._children.length === 3, el('show_poi')._children.length);
  check('the saved-area selector stays usable in the browse mode',
        el('area').disabled === false, el('area').disabled);
  check('compose stays disabled outside the plain area mode',
        el('compose_loops').disabled === true, el('compose_loops').disabled);
  check('the button says what it does',
        el('search').textContent === 'Show the points of interest', el('search').textContent);

  el('results')._children.length = 0;
  await search();
  let plu = lastPoiList();
  check('the browse hits its own endpoint, not /api/hikes', !!plu, fetched.slice(-3));
  check('the browse marks itself for the export', /[?&]pois=true/.test(plu), plu);
  check('with nothing ticked it asks for every kind', !/show_poi=/.test(plu), plu);
  check('the live browse sends the map box', /south=/.test(plu), plu);
  check('walk/gain filters are left out of a listing',
        !/min_gain_m=|max_distance_km=|circular=/.test(plu), plu);
  check('the summary is the status line',
        el('status').textContent === '2 objects: 1 church, 1 ruin', el('status').textContent);
  check('one list entry per object', el('results')._children.length === 2,
        el('results')._children.length);
  check('an unnamed object is labelled by its kind, never blank',
        /ruin/.test(el('results')._children[1].innerHTML), el('results')._children[1].innerHTML);
  check('each object is pinned where it actually is',
        circleMarkers.some(p => Array.isArray(p) && p[0] === 50.7301 && p[1] === 15.6001),
        circleMarkers);
  check('no distance is shown — nothing was measured',
        !/ m\b/.test(el('results')._children[0].innerHTML),
        el('results')._children[0].innerHTML);

  // Picking kinds narrows the listing.
  el('show_poi').selectedOptions = [{ value: 'ruins' }];
  await search();
  plu = lastPoiList();
  check('the chosen kinds reach the query', /show_poi=ruins/.test(plu), plu);

  // The downloaded-area half of the mode: browse offline, no bbox.
  el('area').value = 'krkonose';
  await search();
  plu = lastPoiList();
  check('browsing a downloaded area sends the area, not a box',
        /area=krkonose/.test(plu) && !/south=/.test(plu), plu);
  check('the offline browse is marked as such', / \[offline\]$/.test(el('status').textContent),
        el('status').textContent);

  // A pre-POI snapshot must not read as "there is nothing here".
  el('area').value = 'oldarea';
  await search();
  check('a pre-POI area says it cannot know, not that the area is empty',
        /saved before the feature existed/.test(el('status').textContent),
        el('status').textContent);
  check('and does not send the user hunting for other kinds',
        !/pick other kinds/.test(el('status').textContent), el('status').textContent);

  // The GPX/GeoJSON buttons replay the stored params, so the file matches the list.
  global.window.location = '';
  download('gpx');
  check('the export replays the browse params',
        /^\/api\/gpx\?.*pois=true/.test(global.window.location), global.window.location);
  check('the export targets the same area', /area=oldarea/.test(global.window.location),
        global.window.location);

  // 12. The finer coverage gap: a file FULL of objects that never looked for the kind
  // being asked about. `stale_area` is false there, so the pre-POI branch above cannot
  // cover it — which is exactly why `area_gap` exists beside it.
  el('area').value = 'oldkinds';
  el('show_poi').selectedOptions = [{ value: 'castle' }];
  el('results')._children.length = 0;
  await search();          // no `show_poi=ruins` in the canned reply => the empty shape
  check('a kind newer than the area is reported as a fact about the file',
        /predates the kind tree/.test(el('status').textContent), el('status').textContent);
  check('and never as "nothing of that kind is mapped here"',
        !/mapped in OSM here/.test(el('status').textContent), el('status').textContent);

  el('show_poi').selectedOptions = [{ value: 'ruins' }];
  el('results')._children.length = 0;
  await search();          // the partial shape: objects came back AND a kind is missing
  check('the gap is still reported when some objects did come back',
        /predates the kind tree/.test(el('status').textContent), el('status').textContent);
  check('and the objects it does carry are still shown and summarised',
        /1 object: 1 ruin/.test(el('status').textContent) &&
        el('results')._children.length === 1,
        [el('status').textContent, el('results')._children.length]);

  // 13. The saved-area cards. Each coverage state gets its own warning, and the current
  // one gets none — a warning that never turns off is just noise.
  // The stub's `appendChild` pushes onto `_children`, and the page clears the list with
  // `innerHTML = ''` — which the stub does not translate into dropping them. So reset it
  // by hand, or `cards` silently holds every card rendered since startup and the indices
  // below point at the wrong areas.
  el('areas_list')._children.length = 0;
  renderAreaList(CARD_AREAS);
  const cards = el('areas_list')._children.map(c => c.innerHTML);
  check('a current area carries no coverage warning',
        !/re-download/.test(cards[0]), cards[0]);
  check('an area behind the registry names how many kinds and which',
        /predates 2 kind\(s\)/.test(cards[1]) && /tree, mill/.test(cards[1]), cards[1]);
  check('an area that records no kind set says exactly that',
        /kinds not recorded/.test(cards[2]), cards[2]);
  check('and the pre-POI wording is reserved for a file with no objects at all',
        !/no points of interest/.test(cards[1]) && /no points of interest/.test(cards[3]),
        [cards[1], cards[3]]);

  // 14. What the SOURCE could not answer. `/api/hikes` answers with an envelope now
  // (routes + notices), and the two notice kinds must NOT be rendered the same way.
  // The stub's `appendChild` pushes onto `_children` and the page clears with
  // `innerHTML = ''`, which the stub does not translate into dropping them — so both
  // lists are reset by hand before each search, or a stale child answers for a new one.
  el('mode').value = 'area';
  updateMode();
  // Drop the destination filter left over from §7 — with it on, the page's empty-result
  // sentence is the POI one, and "no_routes displaced 'widen the map'" would pass on a
  // sentence that was never going to say it.
  el('poi').selectedOptions = [];
  el('area').value = 'nofer';
  el('ferrata').value = 'false';
  el('notices')._children.length = 0;
  el('results')._children.length = 0;
  await search();
  check('the ferrata filter reaches the query', /ferrata=false/.test(lastHikes()), lastHikes());
  check('a ferrata gap is shown, not swallowed',
        el('notices')._children.length === 1
        && /NOT a report that the routes are free of cable/
             .test(el('notices')._children[0].textContent),
        el('notices')._children.map(c => c.textContent));
  check('and it stays visible with routes still listed — never gated on an empty result',
        el('results')._children.length === 1, el('results')._children.length);
  check('the status line still reports what did come back',
        /1 match\(es\)/.test(el('status').textContent), el('status').textContent);

  // The other kind: it REPLACES the empty-result advice rather than joining it. Saying
  // both would put "nothing is mapped here" and "widen the map" on screen at once.
  el('area').value = 'norelations';
  el('notices')._children.length = 0;
  el('results')._children.length = 0;
  await search();
  check('no route relations is reported as a fact about the map',
        /not your filters/.test(el('status').textContent), el('status').textContent);
  check('and it displaces the "widen the map" advice instead of sitting beside it',
        !/widen the map/.test(el('status').textContent), el('status').textContent);
  check('that one is not ALSO rendered as a notice',
        el('notices')._children.length === 0, el('notices')._children.length);

  // And the quiet direction, which is what makes either one a signal: a search with
  // nothing to caveat renders no notice at all.
  el('area').value = 'krkonose';
  el('ferrata').value = '';
  el('notices')._children.length = 0;
  await search();
  check('a search with nothing to say says nothing',
        el('notices')._children.length === 0, el('notices')._children.length);
  check('and falls back to the ordinary empty-result advice',
        /widen the map/.test(el('status').textContent), el('status').textContent);

  console.log('PASS ' + ok.length + ' / FAIL ' + fail.length);
  fail.forEach(f => console.log('  FAIL: ' + f));
  process.exit(fail.length ? 1 : 0);
})();
