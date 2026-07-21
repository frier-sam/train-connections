/* iTrainConnections — interactive map of India's train network, by CITY.
   Stations are clustered into cities (Delhi = New Delhi + Nizamuddin + ...),
   so reachability and journeys work city-to-city and a transfer may arrive at
   one station of a city and leave from another. Routes are still drawn on the
   real per-station track geometry. */

const map = L.map('map', { preferCanvas: true, zoomControl: true, minZoom: 4, maxZoom: 12 })
  .setView([22.6, 80.0], 5);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; OpenStreetMap &copy; CARTO', subdomains: 'abcd', maxZoom: 12,
}).addTo(map);

// Routes/origin markers draw in their own pane ABOVE the city dots but with
// pointer-events off — otherwise their canvas would sit over the dot canvas
// and permanently swallow every hover/click meant for the city markers.
const routePane = map.createPane('routes');
routePane.style.zIndex = 450;            // overlay pane is 400, markers 600
routePane.style.pointerEvents = 'none';
const routeRenderer = L.canvas({ padding: 0.5, pane: 'routes' });
const dotRenderer = L.canvas({ padding: 0.5 });

let ST, TR, CT;              // station rows, train rows, city rows
let stCity = [];             // station idx -> city id
let cityTrains = [];         // city id -> Set(train idx)
let trainCities = [];        // train idx -> ordered unique city ids along the route
let dots = [], hubMarkers = [], hubIdx = [];
const routeLayer = L.layerGroup().addTo(map);
let activeTypes = null;
const state = { from: null, to: null, active: 'from' };
const $ = s => document.querySelector(s);

// ---- accessors ----
const s_name = i => ST[i][1], s_lat = i => ST[i][4], s_lng = i => ST[i][5];
const t_num = t => TR[t][0], t_name = t => TR[t][1], t_type = t => TR[t][2],
      t_dist = t => TR[t][4], t_dur = t => TR[t][5], t_stops = t => TR[t][6];
const c_name = c => CT[c][0], c_lat = c => CT[c][1], c_lng = c => CT[c][2],
      c_state = c => CT[c][3], c_deg = c => CT[c][4], c_trn = c => CT[c][5],
      c_hub = c => CT[c][6], c_nst = c => CT[c][7];

// ---- text helpers ----
const esc = s => String(s).replace(/[&<>"']/g, ch =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
const title = s => String(s).toLowerCase().replace(/(^|[\s\-(])\w/g, ch => ch.toUpperCase());
const cityLabel = c => title(c_name(c));
const durStr = m => m > 0 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m` : '';
const figStr = t => [t_num(t) && '', t_dist(t) > 0 ? `${t_dist(t).toLocaleString()} km` : '', durStr(t_dur(t)), `${t_stops(t).length} stops`]
  .filter(Boolean).join(' · ');

// ---- palette: cool luminance ramp for reach + warm board-yellow hubs ----
const colorFor = d => d > 1200 ? '#a5f3fc' : d > 600 ? '#38e0d0' : d > 250 ? '#3aa0e6' : '#3a6b8a';
const radiusFor = d => d > 1200 ? 3.4 : d > 600 ? 2.6 : d > 250 ? 1.9 : 1.3;
const HUBCOL = '#f7c948';
const hubRadius = trn => 3 + Math.min(4.5, trn / 90);
const ROUTE = '#67e8ff', C_FROM = '#34d18d', C_TO = '#f0536b', C_LEG2 = '#f7c948';
const GHOST = 0.06;

const sll = i => [s_lat(i), s_lng(i)];
const cll = c => [c_lat(c), c_lng(c)];
function gc(a, b) {
  const R = 6371, r = Math.PI / 180;
  const dLat = (c_lat(b) - c_lat(a)) * r, dLng = (c_lng(b) - c_lng(a)) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(c_lat(a) * r) * Math.cos(c_lat(b) * r) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

const DATA_V = '11';   // bump with the ?v= on app.js whenever data is rebuilt
async function boot() {
  const [sj, tj, cj] = await Promise.all([
    fetch(`data/stations.json?v=${DATA_V}`).then(r => r.json()),
    fetch(`data/trains.json?v=${DATA_V}`).then(r => r.json()),
    fetch(`data/cities.json?v=${DATA_V}`).then(r => r.json()),
  ]);
  ST = sj.rows; TR = tj.rows; CT = cj.rows;
  stCity = ST.map(r => r[10]);

  cityTrains = CT.map(() => new Set());
  trainCities = TR.map(tr => {
    const seq = [];
    for (const s of tr[6]) { const c = stCity[s]; if (c >= 0 && seq[seq.length - 1] !== c) seq.push(c); }
    return seq;
  });
  trainCities.forEach((seq, t) => { for (const c of new Set(seq)) cityTrains[c].add(t); });
  hubIdx = CT.map((_, c) => c).filter(c => c_hub(c));

  drawCities();
  buildTypeFilter();
  wireUI();
  // recentre the whole-network view into the area not covered by the panel
  if (panelPx()) map.panBy([-panelPx() / 2, 0], { animate: false });
  readURL();
  syncFields(); syncActive(); render();
  $('#stats').textContent = `${CT.length.toLocaleString()} cities · ${TR.length.toLocaleString()} trains`;
  $('#loading').classList.add('done');
}

// ---- base map (one marker per city) ----
function drawCities() {
  CT.forEach((_, c) => {
    if (c_hub(c)) return;
    const m = L.circleMarker(cll(c), {
      renderer: dotRenderer, radius: radiusFor(c_deg(c)),
      fillColor: colorFor(c_deg(c)), fillOpacity: 0.72, stroke: false,
    });
    m.on('click', () => pick(c));
    m.bindTooltip(cityTip(c), { sticky: true, direction: 'top' });
    m.addTo(map);
    dots[c] = m;
  });
  hubIdx.forEach(c => {
    const m = L.circleMarker(cll(c), {
      renderer: dotRenderer, radius: hubRadius(c_trn(c)),
      fillColor: HUBCOL, fillOpacity: 0.92, color: '#fff', weight: 1, opacity: 0.6,
    });
    m.on('click', () => pick(c));
    m.bindTooltip('★ ' + cityTip(c), { sticky: true, direction: 'top' });
    m.addTo(map);
    hubMarkers[c] = m;
  });
}
const cityTip = c => `${cityLabel(c)} — ${c_deg(c)} cities` + (c_nst(c) > 1 ? ` · ${c_nst(c)} stations` : '');

const markerFor = c => hubMarkers[c] || dots[c];
function setOpacity(c, o) {
  const m = markerFor(c);
  if (m) m.setStyle(hubMarkers[c] ? { fillOpacity: o, opacity: Math.min(1, o) } : { fillOpacity: o });
}
function dimAll(o) { CT.forEach((_, c) => { if (markerFor(c)) setOpacity(c, o); }); }
function resetOpacity() { CT.forEach((_, c) => { if (markerFor(c)) setOpacity(c, c_hub(c) ? 0.92 : 0.72); }); }

// Defensive: never draw an implausible chord. Consecutive stops more than
// MAX_SEG_KM apart (mis-ordered or missing data) split the line instead of
// connecting straight across the country.
const MAX_SEG_KM = 150;
function ptKm(a, b) {
  const r = Math.PI / 180;
  const dLat = (b[0] - a[0]) * r, dLng = (b[1] - a[1]) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin(dLng / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(h));
}
function splitPath(pts) {
  const runs = [];
  let run = [pts[0]];
  for (let i = 1; i < pts.length; i++) {
    if (ptKm(pts[i - 1], pts[i]) > MAX_SEG_KM) { if (run.length > 1) runs.push(run); run = []; }
    run.push(pts[i]);
  }
  if (run.length > 1) runs.push(run);
  return runs;
}

// paths: one latlng array, or an array of arrays (batched multi-polyline)
function drawRoute(paths, color, weight = 1.1, opacity = 0.5) {
  const flat = (Array.isArray(paths[0][0]) ? paths : [paths]).flatMap(splitPath);
  if (!flat.length) return;
  L.polyline(flat, { renderer: routeRenderer, color, weight: weight + 3, opacity: 0.05 }).addTo(routeLayer);
  L.polyline(flat, { renderer: routeRenderer, color, weight, opacity }).addTo(routeLayer);
}

const typePasses = t => !activeTypes || activeTypes.has(t_type(t) || '·');
const trainsAt = c => [...cityTrains[c]].filter(typePasses);

// ---- selection / active-field management ----
function commit(preferred) {
  state.active = state.from == null ? 'from' : state.to == null ? 'to' : (preferred || state.active);
  syncFields(); syncActive(); writeURL(); render();
}
function pick(c) { state[state.active] = c; commit(); }
function selectInto(f, c) { state[f] = c; commit(f); }

// ---- shareable URL state ----
function writeURL() {
  const p = new URLSearchParams();
  if (state.from != null) p.set('from', c_name(state.from));
  if (state.to != null) p.set('to', c_name(state.to));
  history.replaceState(null, '', p.size ? '#' + p.toString() : location.pathname);
}
function readURL() {
  const p = new URLSearchParams(location.hash.slice(1));
  for (const f of ['from', 'to']) {
    const name = p.get(f);
    if (!name) continue;
    const c = CT.findIndex(r => r[0] === name);
    if (c >= 0) state[f] = c;
  }
  if (state.from != null) state.active = state.to == null ? 'to' : 'from';
}

function syncFields() {
  for (const f of ['from', 'to']) {
    const wrap = document.querySelector(`.field[data-field="${f}"]`);
    const inp = $('#' + f);
    if (state[f] != null) { inp.value = cityLabel(state[f]); wrap.classList.add('filled'); }
    else { inp.value = ''; wrap.classList.remove('filled'); }
  }
}
function syncActive() {
  document.querySelectorAll('.field').forEach(el => el.classList.toggle('active', el.dataset.field === state.active));
}

function render() {
  routeLayer.clearLayers();
  resetOpacity();
  const { from, to } = state;
  if (from != null && to != null && from !== to) renderJourney(from, to);
  else if (from != null) renderCity(from);
  else if (to != null) renderCity(to);
  else renderHome();
}

// ---- home / empty state ----
function renderHome() {
  const starters = [
    ['NEW DELHI', 'MUMBAI CST', 'New Delhi', 'Mumbai'],
    ['HOWRAH JN', 'CHENNAI CENTRAL', 'Howrah', 'Chennai'],
    ['BANGALORE CITY JN', 'JAIPUR', 'Bengaluru', 'Jaipur'],
  ].map(([a, b, la, lb]) => ({ a: cityOfStation(a), b: cityOfStation(b), la, lb }))
    .filter(s => s.a != null && s.b != null && s.a !== s.b);
  $('#body').innerHTML =
    '<div class="hint">Pick a <b>From</b> city to see everywhere it reaches.<br>' +
    'Add a <b>To</b> city to find direct trains and one-change routes through hub cities.</div>' +
    (starters.length ? '<div class="starters"><div class="shead">Try a journey</div>' +
      starters.map((s, i) =>
        `<button class="starter" data-i="${i}"><span class="fr">${esc(s.la)}</span><span class="ar">→</span><span class="tt">${esc(s.lb)}</span></button>`
      ).join('') + '</div>' : '');
  document.querySelectorAll('.starter').forEach(btn => btn.onclick = () => {
    const s = starters[+btn.dataset.i];
    state.from = s.a; state.to = s.b; commit('from');
  });
}
function cityOfStation(name) {
  for (let i = 0; i < ST.length; i++) if (s_name(i) === name && stCity[i] >= 0) return stCity[i];
  return null;
}

// ---- single city: everywhere it reaches ----
function renderCity(c) {
  const trains = trainsAt(c);
  const dest = new Set();
  dimAll(GHOST);
  const paths = [];
  for (const t of trains) {
    for (const cc of trainCities[t]) dest.add(cc);
    paths.push(t_stops(t).map(sll));
  }
  if (paths.length) drawRoute(paths, ROUTE);
  dest.delete(c);
  originMarker(c, c_hub(c) ? HUBCOL : '#fff');
  setOpacity(c, 1);
  fly(c);

  $('#body').innerHTML =
    `<div class="boards"><span class="board">${esc(c_name(c))}</span></div>` +
    `<p class="meta">${[esc(title(c_state(c))), c_hub(c) && '<span class="hubtag">★ major hub</span>', c_nst(c) > 1 && `${c_nst(c)} stations`].filter(Boolean).join(' · ')}</p>` +
    `<p class="stat"><b>${dest.size.toLocaleString()}</b> cities reachable direct · <b>${trains.length}</b> trains</p>` +
    `<div class="sec-title">Trains serving this city <span class="n">${trains.length}</span></div>` +
    `<div class="list" id="tlist"></div>`;
  const tlist = $('#tlist');
  if (trains.length) fillTrainList(tlist, trains);
  else tlist.innerHTML = '<div class="empty">No trains match the current type filter — switch some types back on below.</div>';
}

function fillTrainList(box, trains) {
  trains.slice().sort((a, b) => t_dist(b) - t_dist(a)).forEach(t => {
    const el = document.createElement('div');
    el.className = 'train';
    el.innerHTML =
      `<div class="top"><span class="num">${esc(t_num(t))}</span><span class="nm">${esc(title(t_name(t)))}</span>` +
      `<span class="badge" data-t="${esc(t_type(t))}">${esc(t_type(t) || '—')}</span></div>` +
      `<div class="fig">${esc(figStr(t))}</div>`;
    el.onclick = () => { toggleTrain(el); isolateTrain(t); };
    box.appendChild(el);
  });
}

// ---- journey between two cities ----
// station-index slice you'd ride from city cFrom to city cTo on train t.
// Trains can visit a city more than once (loops/reversals): pick the shortest
// valid window over all occurrence pairs.
function legStops(t, cFrom, cTo) {
  const stops = t_stops(t);
  let best = null;
  let i0 = -1;
  for (let k = 0; k < stops.length; k++) {
    const c = stCity[stops[k]];
    if (c === cFrom) i0 = k;                    // restart at the latest From occurrence
    else if (c === cTo && i0 >= 0) {
      if (!best || (k - i0) < (best[1] - best[0])) best = [i0, k];
      i0 = -1;
    }
  }
  return best ? stops.slice(best[0], best[1] + 1) : null;
}
const reachesInOrder = (t, a, b) => { const cs = trainCities[t]; const i = cs.indexOf(a); return i >= 0 && cs.indexOf(b, i + 1) >= 0; };

function renderJourney(a, b) {
  const direct = trainsAt(a).filter(t => reachesInOrder(t, a, b));
  const down = reach(a, 'down'), up = reach(b, 'up');
  const straight = gc(a, b);
  const hops = [];
  for (const h of hubIdx) {
    if (h === a || h === b) continue;
    if (!down.has(h) || !up.has(h)) continue;
    const l1 = down.get(h), l2 = up.get(h);
    if (l1 === l2) continue;   // same train both legs = already a direct train
    hops.push({ h, l1, l2, detour: gc(a, h) + gc(h, b) });
  }
  hops.sort((x, y) => x.detour - y.detour);
  const topHops = hops.slice(0, 12);

  dimAll(GHOST);
  const paths = direct.map(t => legStops(t, a, b)).filter(Boolean).map(s => s.map(sll));
  if (paths.length) drawRoute(paths, C_FROM, 1.6, 0.65);
  topHops.forEach(({ h }) => setOpacity(h, 0.9));
  originMarker(a, C_FROM); originMarker(b, C_TO);
  setOpacity(a, 1); setOpacity(b, 1);
  fitTo([a, b]);

  $('#body').innerHTML =
    `<div class="boards"><span class="board">${esc(c_name(a))}</span><span class="arr">→</span><span class="board">${esc(c_name(b))}</span></div>` +
    `<p class="meta">direct = no change · via a hub = one change</p>` +
    `<div class="sec-title">Direct trains <span class="n">${direct.length}</span></div>` +
    `<div class="list" id="dlist"></div>` +
    `<div class="sec-title">One change, via a hub city <span class="n">${topHops.length}${hops.length > topHops.length ? '+' : ''}</span></div>` +
    `<div class="list" id="hlist"></div>`;

  const dlist = $('#dlist');
  if (direct.length) fillTrainList(dlist, direct);
  else dlist.innerHTML = '<div class="empty">No direct train — try a one-change route below.</div>';

  const hlist = $('#hlist');
  if (!topHops.length) hlist.innerHTML = '<div class="empty">No one-change hub route found.</div>';
  topHops.forEach(({ h, l1, l2, detour }) => {
    const el = document.createElement('div');
    el.className = 'hop';
    const extra = straight > 0 ? Math.round((detour / straight - 1) * 100) : 0;
    el.innerHTML = `<div class="via">via <b>${esc(cityLabel(h))}</b> <span class="badge">${c_trn(h)} trains${extra > 5 ? ` · +${extra}%` : ''}</span></div>` +
      `<div class="legs"><span class="leg1"><span class="num">${esc(t_num(l1))}</span> ${esc(title(t_name(l1)))}</span> → ` +
      `<span class="leg2"><span class="num">${esc(t_num(l2))}</span> ${esc(title(t_name(l2)))}</span></div>`;
    el.onclick = () => {
      const on = el.classList.toggle('on');
      document.querySelectorAll('.hop').forEach(e => { if (e !== el) e.classList.remove('on'); });
      if (on) drawHop(a, h, b, l1, l2); else renderJourney(a, b);
    };
    hlist.appendChild(el);
  });
}

// city -> representative train, over cities reachable downstream/upstream.
// A train may pass a city more than once — union the segments at every occurrence.
function reach(city, dir) {
  const m = new Map();
  for (const t of trainsAt(city)) {
    const cs = trainCities[t];
    for (let k = 0; k < cs.length; k++) {
      if (cs[k] !== city) continue;
      const seg = dir === 'down' ? cs.slice(k + 1) : cs.slice(0, k);
      for (const c of seg) if (!m.has(c)) m.set(c, t);
    }
  }
  return m;
}

function drawHop(a, h, b, l1, l2) {
  routeLayer.clearLayers();
  dimAll(GHOST);
  const s1 = legStops(l1, a, h), s2 = legStops(l2, h, b);
  if (s1) drawRoute(s1.map(sll), C_FROM, 2.4, 0.95);
  if (s2) drawRoute(s2.map(sll), C_LEG2, 2.4, 0.95);
  originMarker(a, C_FROM); originMarker(h, HUBCOL); originMarker(b, C_TO);
  [a, h, b].forEach(x => setOpacity(x, 1));
  fitTo([a, h, b]);
}

// ---- single-train isolation ----
function toggleTrain(el) {
  const on = el.classList.contains('on');
  document.querySelectorAll('.train').forEach(e => e.classList.remove('on'));
  el.classList.toggle('on', !on);
}
function isolateTrain(t) {
  if (!document.querySelector('.train.on')) { render(); return; }
  routeLayer.clearLayers();
  dimAll(GHOST);
  drawRoute(t_stops(t).map(sll), '#ff9f1c', 2.4, 0.95);
  for (const c of trainCities[t]) setOpacity(c, 0.85);
  if (state.from != null) originMarker(state.from, C_FROM);
  if (state.to != null) originMarker(state.to, C_TO);
}

// ---- helpers ----
function originMarker(c, color) {
  L.circleMarker(cll(c), { renderer: routeRenderer, radius: 8, fillColor: color, fillOpacity: 1, stroke: true, color: '#0a0d14', weight: 2 }).addTo(routeLayer);
}
const REDUCE_MOTION = matchMedia('(prefers-reduced-motion: reduce)').matches;

// On desktop the map is full-bleed BEHIND the panel, so focus must be offset
// into the uncovered area. On phones the map container sits entirely above
// the bottom-sheet panel — no offset needed there.
const onPhone = () => window.innerWidth <= 640;
const panelPx = () => (onPhone() ? 0 : $('#panel').offsetWidth);
function fly(c) {
  const z = Math.max(map.getZoom(), 6);
  const ctr = map.unproject(map.project(L.latLng(cll(c)), z).subtract([panelPx() / 2, 0]), z);
  map.flyTo(ctr, z, { duration: 0.5, animate: !REDUCE_MOTION });
}
function fitTo(cs) {
  const pad = { paddingTopLeft: [panelPx() + 28, 28], paddingBottomRight: [28, 28] };
  map.flyToBounds(L.latLngBounds(cs.map(cll)), { ...pad, duration: 0.5, maxZoom: 8, animate: !REDUCE_MOTION });
}

// ---- type filter ----
function buildTypeFilter() {
  const counts = {};
  TR.forEach((_, t) => { const k = t_type(t) || '·'; counts[k] = (counts[k] || 0) + 1; });
  const types = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  activeTypes = new Set(types);
  types.forEach(k => {
    const c = document.createElement('button');
    c.className = 'chip on';
    c.setAttribute('aria-pressed', 'true');
    c.innerHTML = `${esc(k === '·' ? '—' : k)} <span class="ct">${counts[k]}</span>`;
    c.onclick = () => {
      c.classList.toggle('on');
      const on = c.classList.contains('on');
      c.setAttribute('aria-pressed', String(on));
      if (on) activeTypes.add(k); else activeTypes.delete(k);
      render();
    };
    $('#types').appendChild(c);
  });
}

// ---- search: cities by city name OR any member station name ----
let searchIdx = null;
function buildSearchIdx() {
  const cities = CT.map((r, c) => ({ c, q: r[0].toLowerCase() }));
  const stations = [];
  for (let i = 0; i < ST.length; i++) {
    if (stCity[i] < 0) continue;
    const q = s_name(i).toLowerCase();
    if (q === c_name(stCity[i]).toLowerCase()) continue;
    stations.push({ c: stCity[i], q, st: s_name(i) });
  }
  searchIdx = { cities, stations };
}
function searchCities(q) {
  if (!searchIdx) buildSearchIdx();
  const seen = new Map();  // city -> {c, via, rank}
  const consider = (c, rank, via) => {
    const cur = seen.get(c);
    if (!cur || rank < cur.rank) seen.set(c, { c, via, rank });
  };
  for (const e of searchIdx.cities) {
    if (e.q.startsWith(q)) consider(e.c, 0, null);
    else if (e.q.includes(q)) consider(e.c, 2, null);
  }
  for (const e of searchIdx.stations) {
    if (e.q.startsWith(q)) consider(e.c, 1, e.st);
    else if (e.q.includes(q)) consider(e.c, 3, e.st);
  }
  return [...seen.values()]
    .sort((x, y) => (x.rank - y.rank) || (c_hub(y.c) - c_hub(x.c)) || (c_deg(y.c) - c_deg(x.c)))
    .slice(0, 12);
}

// ---- field wiring: autocomplete with full keyboard support ----
function wireUI() {
  ['from', 'to'].forEach(f => {
    const wrap = document.querySelector(`.field[data-field="${f}"]`);
    const inp = $('#' + f);
    const results = wrap.querySelector('.results');
    let hits = [], sel = -1;

    const close = () => {
      results.classList.remove('show');
      inp.setAttribute('aria-expanded', 'false');
      inp.removeAttribute('aria-activedescendant');
      sel = -1;
    };
    const paint = () => {
      results.innerHTML = '';
      hits.forEach((h, i) => {
        const li = document.createElement('li');
        li.id = `${f}-opt-${i}`;
        li.setAttribute('role', 'option');
        li.setAttribute('aria-selected', String(i === sel));
        li.classList.toggle('sel', i === sel);
        li.innerHTML =
          `<span class="nm">${c_hub(h.c) ? '<span class="star">★</span> ' : ''}${esc(cityLabel(h.c))}` +
          `${h.via ? ` <span class="via-st">· ${esc(title(h.via))}</span>` : ''}</span>` +
          `<span class="code">${c_deg(h.c)} cities</span>`;
        li.onmousedown = e => e.preventDefault();   // keep input focus
        li.onclick = () => { close(); selectInto(f, h.c); };
        results.appendChild(li);
      });
      results.classList.toggle('show', hits.length > 0);
      inp.setAttribute('aria-expanded', String(hits.length > 0));
      if (sel >= 0) inp.setAttribute('aria-activedescendant', `${f}-opt-${sel}`);
      else inp.removeAttribute('aria-activedescendant');
      results.querySelector('.sel')?.scrollIntoView({ block: 'nearest' });
    };

    inp.addEventListener('focus', () => { state.active = f; syncActive(); });
    inp.addEventListener('input', () => {
      const q = inp.value.trim().toLowerCase();
      if (q.length < 2) { hits = []; close(); results.innerHTML = ''; return; }
      hits = searchCities(q); sel = -1; paint();
    });
    inp.addEventListener('keydown', e => {
      if (e.key === 'Escape') { close(); return; }
      if (!hits.length || !results.classList.contains('show')) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); sel = (sel + 1) % hits.length; paint(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); sel = (sel - 1 + hits.length) % hits.length; paint(); }
      else if (e.key === 'Enter') { e.preventDefault(); const h = hits[Math.max(sel, 0)]; close(); selectInto(f, h.c); }
    });
    inp.addEventListener('blur', () => setTimeout(close, 150));
  });

  document.querySelectorAll('.field .x').forEach(btn => btn.onclick = () => {
    state[btn.dataset.clear] = null; state.active = btn.dataset.clear; commit();
    $('#' + btn.dataset.clear).focus();
  });
  $('#swap').onclick = () => { [state.from, state.to] = [state.to, state.from]; commit(); };
  document.addEventListener('click', e => {
    if (!e.target.closest('.field')) document.querySelectorAll('.results').forEach(r => r.classList.remove('show'));
  });
}

boot().catch(e => {
  const el = $('#loading');
  el.innerHTML = `<div class="board-lg">Signal failure</div><p>Could not load the network data (${esc(e.message)}).<br>Check your connection and reload the page.</p>`;
});
