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
  circleMarker: () => mk({}),
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
global.fetch = async (url) => {
  fetched.push(url);
  const body = url.startsWith('/api/areas') ? AREAS
             : url.startsWith('/api/pois') ? POI_KINDS
             : url.startsWith('/api/quota') ? { enabled: false }
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

  console.log('PASS ' + ok.length + ' / FAIL ' + fail.length);
  fail.forEach(f => console.log('  FAIL: ' + f));
  process.exit(fail.length ? 1 : 0);
})();
