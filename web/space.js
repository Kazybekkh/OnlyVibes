/* OnlyVibes — live space view. Vanilla JS, no libraries.
 *
 * Data: WS /view frames {"type":"state","ts","laptops":[...],"team_summary"?,"alerts"?}
 * (team_summary/alerts are optional — used when present). Falls back to built-in demo
 * data when opened from file:// or when no server answers within 2.5 s.
 *
 * SECURITY: everything coming from the server is untrusted text. It is only ever
 * inserted with textContent / setAttribute — never innerHTML.
 */
(() => {
'use strict';

// ------------------------------------------------------------------ dom
const $ = (sel, el) => (el || document).querySelector(sel);
const scene = $('#scene');
const devicesEl = $('#devices');
const linesSvg = $('#lines');
const avatarsEl = $('#avatars');
const inSpaceEl = $('#in-space');
const ovDevices = $('#ov-devices');
const coordX = $('#coord-x');
const coordY = $('#coord-y');
const coordZ = $('#coord-z');
const pillText = $('#pill-text');
const connText = $('#conn-text');
const connDot = $('#conn-dot');
const footerConn = $('#footer-conn');
const timelineEl = $('#timeline');
const contextText = $('#context-text');
const agentsOnlineEl = $('#agents-online');
const cardsEl = $('#cards');
const sessionIdEl = $('#session-id');
const zoomPct = $('#zoom-pct');
const pauseBtn = $('#pause-btn');
const focusBtn = $('#focus-btn');

const REDUCED = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
const SVGNS = 'http://www.w3.org/2000/svg';

// file:// niceties: point the board / AR links at the sibling files
if (location.protocol === 'file:') {
  $('#rail-board').setAttribute('href', 'index.html');
  $('#rail-ar').setAttribute('href', 'ar.html');
  $('#ar-link').setAttribute('href', 'ar.html');
}

// ------------------------------------------------------------------ camera
const cam = { azimuth: -12, elevation: 48, zoom: 1.3, auto: !REDUCED, dragging: false };
const ELEV_MIN = 35, ELEV_MAX = 65, ZOOM_MIN = 0.5, ZOOM_MAX = 2.5;
let fitScale = 0.5;
let sceneVisible = true;

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const normAz = (a) => { a %= 360; if (a > 180) a -= 360; if (a < -180) a += 360; return a; };

function applyCam() {
  cam.azimuth = normAz(cam.azimuth);
  cam.elevation = clamp(cam.elevation, ELEV_MIN, ELEV_MAX);
  cam.zoom = clamp(cam.zoom, ZOOM_MIN, ZOOM_MAX);
  scene.style.setProperty('--world-scale', String(Math.max(0.1, fitScale) * cam.zoom));
  scene.style.setProperty('--azimuth', cam.azimuth + 'deg');
  scene.style.setProperty('--elevation', cam.elevation + 'deg');
  zoomPct.textContent = Math.round(cam.zoom * 100) + '%';
  $('#zoom-in').disabled = cam.zoom >= ZOOM_MAX;
  $('#zoom-out').disabled = cam.zoom <= ZOOM_MIN;
  coordX.textContent = 'X ' + Math.round(cam.azimuth) + '°';
  coordY.textContent = 'Y ' + Math.round(cam.elevation) + '°';
  coordZ.textContent = 'Z ' + cam.zoom.toFixed(2);
}
function fit() {
  const w = scene.clientWidth || 800;
  const h = scene.clientHeight || 600;
  fitScale = Math.min((w - 32) / 1580, (h - 130) / 1450, 0.85);
  applyCam();
}
if (window.ResizeObserver) new ResizeObserver(fit).observe(scene);
window.addEventListener('resize', fit);

function syncPause() { pauseBtn.textContent = cam.auto ? 'Pause rotation' : 'Resume rotation'; }
function manual(fn) { fn(); cam.auto = false; syncPause(); applyCam(); } // manual control stops auto-rotate
function resetView() { cam.azimuth = -12; cam.elevation = 48; cam.zoom = 1.3; cam.auto = true; syncPause(); applyCam(); }

// auto-rotate: 0.003 deg per ms (one turn every 2 min), frame delta capped at 64 ms
let lastT = performance.now();
function frame(now) {
  const dt = Math.min(64, now - lastT);
  lastT = now;
  if (cam.auto && !cam.dragging && !document.hidden && sceneVisible) {
    cam.azimuth = normAz(cam.azimuth + 0.003 * dt);
    applyCam();
  }
  requestAnimationFrame(frame);
}
if (window.IntersectionObserver) {
  new IntersectionObserver((entries) => { sceneVisible = entries[0].isIntersecting; }).observe(scene);
}

// drag to orbit (primary button, pointer capture)
let dragId = null, lastX = 0, lastY = 0;
scene.addEventListener('pointerdown', (e) => {
  if (e.button !== 0) return;
  dragId = e.pointerId; lastX = e.clientX; lastY = e.clientY;
  cam.dragging = true;
  scene.classList.add('dragging');
  try { scene.setPointerCapture(e.pointerId); } catch (_) { /* already released */ }
});
scene.addEventListener('pointermove', (e) => {
  if (dragId === null || e.pointerId !== dragId) return;
  const dx = e.clientX - lastX, dy = e.clientY - lastY;
  lastX = e.clientX; lastY = e.clientY;
  cam.azimuth = normAz(cam.azimuth + dx * 0.3);
  if (e.pointerType === 'mouse' || e.pointerType === 'pen') {
    cam.elevation = clamp(cam.elevation - dy * 0.15, ELEV_MIN, ELEV_MAX);
  }
  applyCam();
});
const endDrag = (e) => {
  if (dragId === null || e.pointerId !== dragId) return;
  dragId = null; cam.dragging = false;
  scene.classList.remove('dragging');
};
scene.addEventListener('pointerup', endDrag);
scene.addEventListener('pointercancel', endDrag);

// wheel zoom
scene.addEventListener('wheel', (e) => {
  e.preventDefault();
  let dy = e.deltaY;
  if (e.deltaMode === 1) dy *= 16;                       // lines -> px
  else if (e.deltaMode === 2) dy *= scene.clientHeight;   // pages -> px
  manual(() => { cam.zoom = clamp(cam.zoom * Math.exp(-clamp(dy, -200, 200) * 0.002), ZOOM_MIN, ZOOM_MAX); });
}, { passive: false });

// keys while the scene is focused
scene.addEventListener('keydown', (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key;
  if (k === 'ArrowLeft') { e.preventDefault(); manual(() => { cam.azimuth -= 10; }); }
  else if (k === 'ArrowRight') { e.preventDefault(); manual(() => { cam.azimuth += 10; }); }
  else if (k === 'ArrowUp') { e.preventDefault(); manual(() => { cam.elevation += 5; }); }
  else if (k === 'ArrowDown') { e.preventDefault(); manual(() => { cam.elevation -= 5; }); }
  else if (k === ' ' || k === 'Spacebar') { e.preventDefault(); cam.auto = !cam.auto; syncPause(); applyCam(); }
  else if (k === 'Home') { e.preventDefault(); resetView(); }
  else if (k === '+' || k === '=') { e.preventDefault(); manual(() => { cam.zoom = clamp(cam.zoom + 0.15, ZOOM_MIN, ZOOM_MAX); }); }
  else if (k === '-' || k === '_') { e.preventDefault(); manual(() => { cam.zoom = clamp(cam.zoom - 0.15, ZOOM_MIN, ZOOM_MAX); }); }
});

// buttons
$('#zoom-in').addEventListener('click', () => manual(() => { cam.zoom = clamp(cam.zoom + 0.15, ZOOM_MIN, ZOOM_MAX); }));
$('#zoom-out').addEventListener('click', () => manual(() => { cam.zoom = clamp(cam.zoom - 0.15, ZOOM_MIN, ZOOM_MAX); }));
$('#rot-l').addEventListener('click', () => manual(() => { cam.azimuth -= 15; }));
$('#rot-r').addEventListener('click', () => manual(() => { cam.azimuth += 15; }));
pauseBtn.addEventListener('click', () => { cam.auto = !cam.auto; syncPause(); applyCam(); });
$('#reset').addEventListener('click', resetView);
focusBtn.addEventListener('click', () => {
  const on = document.body.classList.toggle('focus');
  focusBtn.querySelector('span').textContent = on ? 'Exit focus view' : 'Enter focus view';
});

fit();
applyCam();
syncPause();
sessionIdEl.textContent = 'ses-' + Math.random().toString(16).slice(2, 8);

// ------------------------------------------------------------------ small dom helpers
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text; // textContent only — server data is untrusted
  return e;
};
const svgUse = (href, cls) => {
  const s = document.createElementNS(SVGNS, 'svg');
  if (cls) s.setAttribute('class', cls);
  const u = document.createElementNS(SVGNS, 'use');
  u.setAttribute('href', href);
  s.appendChild(u);
  return s;
};

// ------------------------------------------------------------------ avatars / icons
const AVATAR_COLORS = [
  ['#a88c7f', '#281e1a'],
  ['#8e9d8a', '#263121'],
  ['#9693af', '#2c2940'],
  ['#a08ba9', '#302437'],
];
const ownerColor = new Map(); // owner -> [bg, fg], stable by first appearance
function colorFor(owner) {
  if (!ownerColor.has(owner)) ownerColor.set(owner, AVATAR_COLORS[ownerColor.size % AVATAR_COLORS.length]);
  return ownerColor.get(owner);
}
function initials(name) {
  const parts = String(name || '?').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}
function agentIcon(name) {
  const n = String(name || '').toLowerCase();
  if (n === 'claude-code' || n === 'claude') return { href: '#claude', cls: 'ic-claude', tint: 't-claude' };
  if (n === 'codex') return { href: '#codex', cls: 'ic-codex', tint: 't-codex' };
  if (n === 'vibe') return { href: '#mistral', cls: 'ic-vibe', tint: 't-vibe' };
  return { href: '#laptop', cls: 'ic-other', tint: '' };
}
function statusDotColor(st) {
  if (st === 'working') return '#f49a83';
  if (st === 'error' || st === 'stuck') return '#e0605a';
  return '#a2bfa7';
}

// ------------------------------------------------------------------ data model
const agentState = (a) => a.state || (Number(a.cpu) > 5 ? 'working' : 'idle');
const lapKey = (lap) => lap.tailscale_ip || lap.laptop || 'unknown';
const mostActive = (lap) => {
  const agents = lap.agents || [];
  if (!agents.length) return null;
  return [...agents].sort((x, y) => {
    const sx = agentState(x) === 'working' ? 0 : 1;
    const sy = agentState(y) === 'working' ? 0 : 1;
    return sx - sy || (Number(y.cpu) || 0) - (Number(x.cpu) || 0);
  })[0];
};
const laptopWorking = (lap) => (lap.agents || []).some((a) => agentState(a) === 'working');

// seats around the table centre (600, 570), radius 424
function seatPositions(n) {
  const pos = [];
  for (let i = 0; i < n; i++) {
    const a = (225 + (i * 360) / n) * Math.PI / 180;
    const x = 600 + 424 * Math.cos(a);
    const y = 570 + 424 * Math.sin(a);
    const turn = normAz(Math.atan2(y - 570, x - 600) * 180 / Math.PI + 270);
    pos.push({ x, y, turn });
  }
  return pos;
}

// ------------------------------------------------------------------ 3D devices
const laptops = new Map(); // key -> { lap, seat, el, ...refs }
const agentHistory = new Map(); // laptop key -> recent distinct log lines (newest last)
let wires = []; // [{path, dot, a, b}]

function buildDevice(seat, index) {
  const dev = el('div', 'device' + (index % 2 ? ' dark' : ''));
  dev.style.setProperty('--x', seat.x + 'px');
  dev.style.setProperty('--y', seat.y + 'px');
  dev.style.setProperty('--turn', seat.turn + 'deg');

  const computer = el('div', 'computer');
  const screen = el('div', 'screen');
  const winbar = el('div', 'winbar');
  for (let i = 0; i < 3; i++) winbar.appendChild(el('i'));
  const winLabel = el('span', 'win-label');
  winbar.appendChild(winLabel);
  const body = el('div', 'screen-body');
  screen.append(winbar, body, el('div', 'glare'));
  const lid = el('div', 'lid');
  lid.appendChild(el('span', null, 'ov✳'));
  const deck = el('div', 'deck');
  deck.appendChild(el('div', 'keys'));
  deck.appendChild(el('div', 'trackpad'));
  computer.append(screen, lid, deck);

  const floatEl = el('div', 'agent-float');
  const tile = el('div', 'icon-tile');
  const tileIcon = svgUse('#laptop');
  tile.appendChild(tileIcon);
  const fmain = el('div', 'float-main');
  const fname = el('div', 'float-name');
  const fline = el('div', 'float-line');
  fmain.append(fname, fline);
  const ind = el('div', 'status-ind');
  const more = el('span', 'more-pill');
  more.style.display = 'none';
  floatEl.append(tile, fmain, ind, more);

  const label = el('div', 'dev-label');
  const av = el('div', 'avatar');
  const lmain = el('div');
  const lname = el('div', 'dl-name');
  const lsub = el('div', 'dl-sub');
  lmain.append(lname, lsub);
  label.append(av, lmain, el('span', 'dl-dot'));

  dev.append(computer, floatEl, label);
  devicesEl.appendChild(dev);
  return { el: dev, winLabel, body, floatEl, tileIcon, fname, fline, ind, more, av, lname, lsub };
}

function setStatus(ind, st) {
  ind.className = 'status-ind st-' + st;
  ind.textContent = '';
  if (st === 'working') for (let i = 0; i < 4; i++) ind.appendChild(document.createElement('i'));
}

function updateDevice(entry, lap) {
  const owner = lap.owner || lap.laptop || '?';
  const key = lapKey(lap);

  // device label
  const [bg, fg] = colorFor(owner);
  entry.av.style.background = bg;
  entry.av.style.color = fg;
  entry.av.textContent = initials(owner);
  entry.lname.textContent = owner;
  entry.lsub.textContent = [lap.laptop, lap.tailscale_ip].filter(Boolean).join(' · ');

  // screen: window bar + last 5 distinct agent lines, newest at the bottom
  entry.winLabel.textContent = (owner + ' / ' + (lap.laptop || '')).slice(0, 26);
  let hist = agentHistory.get(key);
  if (!hist) { hist = []; agentHistory.set(key, hist); }
  for (const a of lap.agents || []) {
    const line = String(a.summary || a.last_log || '').trim();
    if (line && !hist.includes(line)) hist.push(line);
  }
  if (hist.length > 12) hist.splice(0, hist.length - 12);
  entry.body.textContent = '';
  for (const line of hist.slice(-5)) entry.body.appendChild(el('div', 'ln', line));
  entry.body.appendChild(el('span', 'cursor', '_'));

  // agent float = most active agent
  const agents = lap.agents || [];
  const top = mostActive(lap);
  if (top) {
    const st = agentState(top);
    const icon = agentIcon(top.name);
    entry.tileIcon.setAttribute('href', icon.href);
    entry.tileIcon.setAttribute('class', icon.cls);
    entry.floatEl.className = 'agent-float' + (icon.tint ? ' ' + icon.tint : '');
    entry.fname.textContent = top.name || 'agent';
    let line = String(top.summary || top.last_log || 'idle');
    if (line.length > 50) line = line.slice(0, 50) + '…';
    entry.fline.textContent = line;
    setStatus(entry.ind, st);
    entry.more.style.display = agents.length > 1 ? '' : 'none';
    entry.more.textContent = '+' + (agents.length - 1);
  } else {
    entry.floatEl.className = 'agent-float';
    entry.fname.textContent = 'no agents';
    entry.fline.textContent = 'idle';
    setStatus(entry.ind, 'idle');
    entry.more.style.display = 'none';
  }
}

// connection lines between neighbouring seats (geometry rebuilt only when the set changes)
function rebuildWires() {
  linesSvg.textContent = '';
  wires = [];
  const entries = [...laptops.values()];
  const n = entries.length;
  if (n < 2) return;
  for (let i = 0; i < n; i++) {
    const a = entries[i], b = entries[(i + 1) % n];
    const mx = (a.seat.x + b.seat.x) / 2, my = (a.seat.y + b.seat.y) / 2;
    const ang = Math.atan2(my - 570, mx - 600);
    const cx = 600 + 424 * 1.3 * Math.cos(ang);
    const cy = 570 + 424 * 1.3 * Math.sin(ang);
    const path = document.createElementNS(SVGNS, 'path');
    const id = 'wire-' + i;
    path.setAttribute('id', id);
    path.setAttribute('d', 'M ' + a.seat.x + ' ' + a.seat.y + ' Q ' + cx + ' ' + cy + ' ' + b.seat.x + ' ' + b.seat.y);
    path.setAttribute('class', 'wire idle');
    linesSvg.appendChild(path);
    let dot = null;
    if (!REDUCED) {
      dot = document.createElementNS(SVGNS, 'circle');
      dot.setAttribute('r', '5');
      dot.setAttribute('class', 'motion-dot');
      const am = document.createElementNS(SVGNS, 'animateMotion');
      am.setAttribute('dur', '4s');
      am.setAttribute('repeatCount', 'indefinite');
      const mp = document.createElementNS(SVGNS, 'mpath');
      mp.setAttribute('href', '#' + id);
      am.appendChild(mp);
      dot.appendChild(am);
      linesSvg.appendChild(dot);
    }
    wires.push({ path, dot, a, b });
  }
}
function updateWires() {
  for (const w of wires) {
    const active = laptopWorking(w.a.lap) && laptopWorking(w.b.lap);
    w.path.setAttribute('class', 'wire ' + (active ? 'active' : 'idle'));
    if (w.dot) w.dot.style.display = active ? '' : 'none';
  }
}

// ------------------------------------------------------------------ toolbar / cards / flow
function rebuildToolbar(laps) {
  avatarsEl.textContent = '';
  const seenOwners = new Set();
  for (const lap of laps) {
    const owner = lap.owner || lap.laptop || '?';
    if (seenOwners.has(owner)) continue;
    seenOwners.add(owner);
    const [bg, fg] = colorFor(owner);
    const av = el('div', 'av', initials(owner));
    av.style.background = bg;
    av.style.color = fg;
    av.setAttribute('title', owner);
    avatarsEl.appendChild(av);
  }
  inSpaceEl.textContent = laps.length + ' in space';
  ovDevices.textContent = '● ' + laps.length + ' devices connected';
}

function rebuildCards(laps) {
  cardsEl.textContent = '';
  for (const lap of laps) {
    const owner = lap.owner || lap.laptop || '?';
    const [bg, fg] = colorFor(owner);
    const card = el('div', 'card');
    const left = el('div', 'card-left');
    const av = el('div', 'avatar', initials(owner));
    av.style.background = bg;
    av.style.color = fg;
    const lmain = el('div');
    lmain.append(el('div', 'card-name', owner), el('div', 'card-sub', [lap.laptop, lap.tailscale_ip].filter(Boolean).join(' · ')));
    left.append(av, lmain);
    const right = el('div', 'card-agents');
    for (const a of lap.agents || []) {
      const st = agentState(a);
      const icon = agentIcon(a.name);
      const row = el('div', 'ca');
      row.appendChild(svgUse(icon.href, icon.cls));
      row.appendChild(el('span', 'nm', a.name || 'agent'));
      row.appendChild(el('span', null, st));
      const dot = el('span', 'ca-dot');
      dot.style.background = statusDotColor(st);
      row.appendChild(dot);
      right.appendChild(row);
    }
    if (!(lap.agents || []).length) right.appendChild(el('div', 'ca', 'no agents'));
    card.append(left, right);
    cardsEl.appendChild(card);
  }
}

function nowClock() {
  const d = new Date();
  const p = (x) => String(x).padStart(2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes());
}

const seenAlerts = new Set();
const seenAgents = new Map(); // agent key -> summary|state signature
let headItem = null;

function addTimelineItem(item) {
  const node = el('div', 'tl-item');
  const tile = el('div', 'tile' + (item.warn ? ' warn' : item.tint ? ' ' + item.tint : ''));
  if (item.warn) tile.textContent = '⚠';
  else tile.appendChild(svgUse(item.icon.href, item.icon.cls));
  node.appendChild(tile);
  const body = el('div');
  body.appendChild(el('div', 'tl-time', item.time));
  body.appendChild(el('div', 'tl-title', item.title));
  if (item.quote) body.appendChild(el('div', 'tl-quote', item.quote));
  else body.appendChild(el('div', 'tl-text', item.text));
  node.appendChild(body);
  timelineEl.insertBefore(node, timelineEl.firstChild); // newest first
  while (timelineEl.children.length > 8) timelineEl.lastChild.remove();
}

function updateHeadline(text) {
  if (!headItem) {
    headItem = el('div', 'tl-item');
    const tile = el('div', 'tile');
    tile.appendChild(svgUse('#cube'));
    headItem.appendChild(tile);
    const body = el('div');
    body.appendChild(el('div', 'tl-time', 'team'));
    body.appendChild(el('div', 'tl-title', 'The room'));
    headItem.appendChild(body);
    headItem.quoteEl = el('div', 'tl-quote');
    body.appendChild(headItem.quoteEl);
    timelineEl.insertBefore(headItem, timelineEl.firstChild);
  }
  headItem.quoteEl.textContent = text;
}

// ------------------------------------------------------------------ apply one /view frame
let lastKeys = '';
function applyState(msg) {
  if (!msg || msg.type !== 'state') return;
  const laps = (msg.laptops || []).slice(0, 8);
  laps.sort((a, b) => String(a.owner || a.laptop || '').localeCompare(String(b.owner || b.laptop || '')));

  const keys = laps.map(lapKey).join('|');
  if (keys !== lastKeys) {
    lastKeys = keys;
    const alive = new Set(laps.map(lapKey));
    for (const [k, entry] of [...laptops]) {
      if (!alive.has(k)) { entry.el.remove(); laptops.delete(k); agentHistory.delete(k); }
    }
    const seats = seatPositions(laps.length);
    laps.forEach((lap, i) => {
      const k = lapKey(lap);
      let entry = laptops.get(k);
      if (!entry) {
        entry = Object.assign({ lap, seat: seats[i] }, buildDevice(seats[i], i));
        laptops.set(k, entry);
      }
      entry.lap = lap;
      entry.seat = seats[i];
      entry.el.style.setProperty('--x', seats[i].x + 'px');
      entry.el.style.setProperty('--y', seats[i].y + 'px');
      entry.el.style.setProperty('--turn', seats[i].turn + 'deg');
      entry.el.classList.toggle('dark', i % 2 === 1);
      updateDevice(entry, lap);
    });
    rebuildWires();
    rebuildToolbar(laps);
  } else {
    for (const lap of laps) {
      const entry = laptops.get(lapKey(lap));
      if (entry) { entry.lap = lap; updateDevice(entry, lap); }
    }
    updateWires();
    rebuildToolbar(laps);
  }
  rebuildCards(laps);

  const totalAgents = laps.reduce((n, l) => n + (l.agents || []).length, 0);
  const working = laps.reduce((n, l) => n + (l.agents || []).filter((a) => agentState(a) === 'working').length, 0);
  const headline = msg.team_summary || (laps.length + ' laptops · ' + totalAgents + ' agents · ' + working + ' working');
  pillText.textContent = headline;
  updateHeadline(headline);
  contextText.textContent = 'Shared context · ' + laps.length + (laps.length === 1 ? ' laptop' : ' laptops') + ' in the room right now.';
  agentsOnlineEl.textContent = String(totalAgents);

  // alerts -> timeline (deduped by text)
  for (const al of msg.alerts || []) {
    const text = al && al.text ? String(al.text) : '';
    if (text && !seenAlerts.has(text)) {
      seenAlerts.add(text);
      if (seenAlerts.size > 60) seenAlerts.delete(seenAlerts.values().next().value);
      addTimelineItem({ warn: true, time: nowClock(), title: 'Collision alert', text });
    }
  }

  // agent summary/state changes -> timeline
  for (const lap of laps) {
    const owner = lap.owner || lap.laptop || '?';
    for (const a of lap.agents || []) {
      const ak = lapKey(lap) + ':' + (a.agent_id || (a.name + ':' + a.pid));
      const sig = String(a.summary || '') + '|' + agentState(a);
      const prev = seenAgents.get(ak);
      if (prev !== undefined && prev !== sig) {
        addTimelineItem({
          icon: agentIcon(a.name),
          tint: agentIcon(a.name).tint === 't-codex' ? 't-codex' : '',
          time: owner.toUpperCase() + ' · ' + nowClock(),
          title: a.name || 'agent',
          text: String(a.summary || a.last_log || agentState(a)),
        });
      }
      seenAgents.set(ak, sig);
    }
  }
}

// ------------------------------------------------------------------ live connection
let gotMessage = false;
let demoTimer = null;

function setConn(kind, text) {
  connText.textContent = text;
  connDot.className = 'cdot ' + kind;
  footerConn.textContent = text;
}

function connect() {
  if (location.protocol === 'file:') { startDemo(); return; }
  let ws;
  try {
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/view');
  } catch (_) {
    setConn('reconnecting', 'Reconnecting…');
    setTimeout(connect, 2000);
    return;
  }
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (!msg || msg.type !== 'state') return;
    gotMessage = true;
    if (demoTimer) { clearInterval(demoTimer); demoTimer = null; }
    setConn('live', 'All systems vibing');
    applyState(msg);
  };
  ws.onclose = () => {
    setConn('reconnecting', 'Reconnecting…');
    setTimeout(connect, 2000);
  };
  ws.onerror = () => { try { ws.close(); } catch (_) { /* ignore */ } };
}
// (the connection is started at the very bottom of this file, after the demo data below
//  is declared — startDemo() must not run before those `let`/`const`s are initialized)

// ------------------------------------------------------------------ demo data
const DEMO = [
  {
    laptop: 'macbook-pro', owner: 'Doni', tailscale_ip: '100.64.1.1',
    agents: [
      { name: 'claude-code', pid: 101, script: [
        { summary: 'editing web/space.js', state: 'working', cpu: 42.5, last_log: '⏺ Edit web/space.js' },
        { summary: 'running the test suite', state: 'working', cpu: 58.1, last_log: 'Ran pytest — 27 passed' },
        { summary: 'reviewing the git diff', state: 'working', cpu: 31.0, last_log: 'Read git diff HEAD' },
        { summary: 'waiting for review', state: 'idle', cpu: 2.2, last_log: 'Done for now' },
      ] },
      { name: 'vibe', pid: 102, script: [
        { summary: 'watching the fleet', state: 'idle', cpu: 4.0, last_log: 'heartbeat ok' },
        { summary: 'summarizing the room', state: 'working', cpu: 12.8, last_log: '⏺ Read server/hub.py' },
      ] },
    ],
  },
  {
    laptop: 'macbook-air', owner: 'Alex', tailscale_ip: '100.64.1.2',
    agents: [
      { name: 'codex', pid: 201, script: [
        { summary: 'patching three files', state: 'working', cpu: 36.4, last_log: 'patched web/space.css' },
        { summary: 'reading the reporter code', state: 'working', cpu: 48.9, last_log: 'Read reporter/reporter.py' },
        { summary: 'tests failing, retrying', state: 'error', cpu: 22.0, last_log: '3 tests failing' },
      ] },
    ],
  },
  {
    laptop: 'thinkpad', owner: 'Sam', tailscale_ip: '100.64.1.3',
    agents: [
      { name: 'vibe', pid: 301, script: [
        { summary: 'looping on a traceback', state: 'error', cpu: 61.2, last_log: 'Traceback (most recent call last)…' },
        { summary: 'still stuck on the error', state: 'stuck', cpu: 55.7, last_log: 'Exception: boom' },
        { summary: 'fixed the import', state: 'working', cpu: 27.3, last_log: '⏺ Edit worker/worker.py' },
      ] },
    ],
  },
  {
    laptop: 'macbook-pro', owner: 'Maya', tailscale_ip: '100.64.1.4',
    agents: [
      { name: 'claude-code', pid: 401, script: [
        { summary: 'designing the flow panel', state: 'working', cpu: 39.8, last_log: '⏺ Edit web/space.html' },
        { summary: 'writing docs', state: 'working', cpu: 18.6, last_log: '⏺ Write README.md' },
        { summary: 'idle between tasks', state: 'idle', cpu: 1.4, last_log: 'TodoWrite 4/5' },
      ] },
    ],
  },
];
const DEMO_HEADLINES = [
  'Four laptops in the room, all moving.',
  'Sam is stuck — Alex and Doni are heads-down.',
  'Maya is writing docs while the tests run.',
  'The room is calm. Ideas are moving anyway.',
];
const DEMO_ALERTS = [
  [{ kind: 'collision', text: 'codex (Alex) and claude-code (Maya) are both editing web/space.css' }],
  [],
  [{ kind: 'collision', text: 'vibe (Sam) and claude-code (Doni) are both editing server/hub.py' }],
];
let demoTick = 0;
function startDemo() {
  if (demoTimer) return;
  setConn('demo', 'Demo data');
  const apply = () => {
    demoTick++;
    applyState({
      type: 'state',
      ts: Date.now() / 1000,
      team_summary: DEMO_HEADLINES[demoTick % DEMO_HEADLINES.length],
      alerts: DEMO_ALERTS[demoTick % DEMO_ALERTS.length],
      laptops: DEMO.map((l) => ({
        laptop: l.laptop, owner: l.owner, tailscale_ip: l.tailscale_ip, ts: Date.now() / 1000,
        agents: l.agents.map((a) => {
          const s = a.script[demoTick % a.script.length];
          return { name: a.name, pid: a.pid, cpu: s.cpu, cwd: '', last_log: s.last_log, summary: s.summary, state: s.state };
        }),
      })),
    });
  };
  apply();
  demoTimer = setInterval(apply, 3000); // demo data changes every few seconds
}

setTimeout(() => { if (!gotMessage) startDemo(); }, 2500); // no server within 2.5 s -> demo
connect();

})();
