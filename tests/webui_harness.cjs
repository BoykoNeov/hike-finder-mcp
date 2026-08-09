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
    routes: 12, samples: 3198, places: 0, pois: 280, bytes: 225925 },
  { name: 'oldarea',  bbox: [49.90, 13.90, 50.20, 14.20], created_at: '2025-01-02T08:00:00+00:00',
    routes: 4, samples: 900, places: 0, pois: 0, bytes: 40000 },
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
global.fetch = async (url) => {
  fetched.push(url);
  const body = url.startsWith('/api/areas') ? AREAS
             : url.startsWith('/api/pois') ? POI_KINDS
             : url.startsWith('/api/quota') ? { enabled: false }
             : /[?&]to_poi=/.test(url) ? TO_POI_HIKES
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

  console.log('PASS ' + ok.length + ' / FAIL ' + fail.length);
  fail.forEach(f => console.log('  FAIL: ' + f));
  process.exit(fail.length ? 1 : 0);
})();
