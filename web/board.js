/*
 * OnlyVibes board: the live orchestration UI (web/index.html).
 *
 * Run: served by the hub at "/" (cd server && uv run uvicorn main:app --port 8000), or open
 * web/index.html directly for the built-in demo. No build step, no env vars.
 * URL options: ?demo=1 (force demo data; &speed=N runs it N times faster), ?hub=host:port (use another hub; needs CORS),
 * #task=<task id> (open a task's detail drawer).
 *
 * Data flow (PROTOCOL.md §4 only):
 *   WS /board  -> snapshot + incremental frames -> OVLib.applyFrame(store) -> batched render
 *   GET /api/state           initial load / resync while the socket is down
 *   GET /api/tasks/{id}      drawer: task + all transcript events, then live "event" frames (dedupe by seq)
 *   POST /api/projects, /api/messages, /api/projects/{id}/tasks, /api/tasks/{id}/{retry,cancel,done}
 * If the page is opened from file:// or nothing answers within 2.5 s, the same code path is fed by
 * OVDemo (web/demo.js) instead, clearly labelled "Demo data".
 *
 * Security: every agent/user string goes into the DOM as text (h() / textContent). The only
 * innerHTML assignment is setMarkdown(), whose input is escaped first by OVLib.renderMarkdown.
 */
(function () {
  'use strict';

  const L = window.OVLib;
  const $ = (id) => document.getElementById(id);
  const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)');
  const DEMO_AFTER_MS = 2500;
  const now = () => Date.now() / 1000;

  // ================================================================== DOM helpers

  // "MacBook Air · M2 · macOS 27.0" from a worker's optional device info
  function deviceText(d) {
    if (!d) return '';
    return [d.model, (d.chip || '').replace(/^Apple /, ''), d.os].filter(Boolean).join(' · ');
  }

  /** h('div', {class, text, on: {click}, data: {id}, ...attrs}, ...children). Strings become text nodes. */
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k === 'on') for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
        else if (k === 'data') for (const [dk, dv] of Object.entries(v)) el.dataset[dk] = dv;
        else if (k === 'style') el.style.cssText = v;
        else el.setAttribute(k, v === true ? '' : v);
      }
    }
    append(el, children);
    return el;
  }
  function append(el, children) {
    for (const c of children.flat(Infinity)) {
      if (c == null || c === false) continue;
      el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  /** The single innerHTML sink: OVLib.renderMarkdown escapes before it formats. */
  function setMarkdown(el, md) {
    el.innerHTML = L.renderMarkdown(md);
    return el;
  }
  const clear = (el) => { el.replaceChildren(); return el; };

  function agentBadge(agent) {
    const m = L.agentMeta(agent);
    return h('span', { class: `ag ${m.cls}`, title: agent || 'agent', 'aria-hidden': 'true' }, m.letter);
  }

  function statusPill(status) {
    return h('span', { class: `pill st-${status}` }, L.STATUS_LABEL[status] || status);
  }

  function toast(text, kind = 'info') {
    const el = h('div', { class: `toast ${kind}`, role: kind === 'error' ? 'alert' : 'status' }, text);
    $('toasts').appendChild(el);
    setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 220); }, kind === 'error' ? 6000 : 3200);
  }

  // ================================================================== app state

  const params = new URLSearchParams(location.search);
  const HUB = params.get('hub'); // optional host:port override for development
  const app = {
    store: L.createStore(),
    mode: 'connecting', // connecting | live | reconnecting | demo
    hubSeen: false, // has a real hub ever answered?
    demo: null, // OVDemo hub while in demo mode
    projectId: sessionStorage.getItem('ov.project') || null,
    projectPinned: !!sessionStorage.getItem('ov.project'),
    drawer: null, // {taskId, events, renderedSeq, tab, loading, stick, unseen}
    railOpen: false,
    railUnread: 0,
    name: localStorage.getItem('ov.name') || '',
    activity: new Map(), // worker id -> number[] (events per 2.5 s bucket, last 60 s)
  };

  // ================================================================== transport

  function httpBase() {
    if (HUB) return `${location.protocol === 'https:' ? 'https' : 'http'}://${HUB}/`;
    return new URL('./', location.href).href;
  }
  function wsUrl(path) {
    const u = new URL(path, httpBase());
    u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
    return u.href;
  }

  /** Call the hub (or the demo hub). Resolves JSON; rejects Error with .status/.code from {"error": {...}}. */
  async function api(method, path, body) {
    if (app.mode === 'demo' && app.demo) return app.demo.request(method, path, body);
    const res = await fetch(new URL(path.replace(/^\//, ''), httpBase()), {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* empty or non-JSON body */ }
    if (!res.ok) {
      const err = new Error((data && data.error && data.error.message) || `${method} ${path} failed (${res.status})`);
      err.status = res.status;
      err.code = data && data.error && data.error.code;
      throw err;
    }
    return data;
  }

  // ---------------------------------------------------------------- live connection

  let ws = null;
  let backoff = 1000;
  let lastFrameAt = 0;
  let reconnectTimer = null;
  let socketSynced = false; // has the current socket delivered its snapshot?

  function setMode(mode, label) {
    app.mode = mode;
    const conn = $('conn');
    conn.dataset.state = mode;
    $('conn-label').textContent = label;
    conn.title = mode === 'demo'
      ? 'No hub answered, so this board is showing simulated data. It switches to live data as soon as a hub responds.'
      : '';
    document.body.classList.toggle('is-demo', mode === 'demo');
  }

  function connectLive() {
    clearTimeout(reconnectTimer);
    let socket;
    try { socket = new WebSocket(wsUrl('board')); } catch { scheduleReconnect(); return; }
    ws = socket;
    socket.onmessage = (ev) => {
      let frame;
      try { frame = JSON.parse(ev.data); } catch { return; }
      lastFrameAt = Date.now();
      if (frame.type === 'snapshot') { socketSynced = true; onHubAnswered(true); }
      if (app.mode === 'demo') return; // snapshot not seen yet on this socket
      receive(frame);
    };
    socket.onopen = () => { backoff = 1000; lastFrameAt = Date.now(); socketSynced = false; };
    socket.onclose = () => {
      if (ws !== socket) return;
      ws = null;
      socketSynced = false;
      if (app.mode === 'live') setMode('reconnecting', 'Reconnecting');
      scheduleReconnect();
    };
    socket.onerror = () => { /* onclose follows */ };
  }

  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    const wait = app.mode === 'demo' ? 10000 : backoff;
    backoff = Math.min(backoff * 2, 10000);
    reconnectTimer = setTimeout(() => {
      // while the socket is down, keep the board fresh over plain HTTP if the hub answers
      if (app.mode !== 'demo') resyncOverHttp();
      connectLive();
    }, wait);
  }

  async function resyncOverHttp() {
    try {
      const state = await withTimeout(api('GET', '/api/state'), DEMO_AFTER_MS);
      onHubAnswered(false);
      // never let an HTTP snapshot overwrite newer frames from a synced socket
      if (!(ws && ws.readyState === WebSocket.OPEN && socketSynced)) receive({ type: 'snapshot', ...state });
      return true;
    } catch { return false; }
  }

  /** A real hub responded (over the socket or plain HTTP): leave demo mode if needed. */
  function onHubAnswered(viaSocket) {
    app.hubSeen = true;
    if (app.mode === 'demo') {
      app.demo.stop();
      app.demo = null;
      app.store = L.createStore();
      app.activity.clear();
      resetBoardDom();
      toast('Hub found. Showing live data.');
    }
    if (viaSocket) setMode('live', 'Live');
    else if (app.mode === 'demo') setMode('connecting', 'Connecting');
  }

  function startDemo(reason) {
    if (app.mode === 'demo' || app.hubSeen) return;
    setMode('demo', 'Demo data');
    $('conn').title = `${reason}. This board is showing simulated data and switches to live data when a hub answers.`;
    const speed = Math.min(20, Math.max(0.25, Number(params.get('speed')) || 1.5)); // ?speed= fast-forwards the demo
    app.demo = window.OVDemo.createDemoHub({ speed });
    app.demo.start(receive);
  }

  // socket watchdog: the hub pings every 15 s; silence for 45 s means a dead connection
  setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN && Date.now() - lastFrameAt > 45000) ws.close();
  }, 5000);

  function withTimeout(promise, ms) {
    return Promise.race([promise, new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms))]);
  }

  // ================================================================== frames -> state -> render

  const dirty = { fleet: false, board: false, rail: false, drawer: false, switcher: false };
  let rafPending = false;
  function invalidate(...parts) {
    parts.forEach((p) => { dirty[p] = true; });
    if (!rafPending) { rafPending = true; requestAnimationFrame(flush); }
  }
  function flush() {
    rafPending = false;
    ensureProject();
    if (dirty.switcher) { dirty.switcher = false; renderSwitcher(); }
    if (dirty.fleet) { dirty.fleet = false; renderFleet(); }
    if (dirty.board) { dirty.board = false; renderBoard(); }
    if (dirty.rail) { dirty.rail = false; renderRail(); }
    if (dirty.drawer) { dirty.drawer = false; renderDrawer(); }
  }

  function receive(frame) {
    const change = L.applyFrame(app.store, frame);
    if (!change) return;
    switch (change.type) {
      case 'snapshot':
        railRenderedFor = null; // messages were replaced wholesale
        invalidate('switcher', 'fleet', 'board', 'rail', 'drawer');
        if (app.drawer) loadDrawerTask(app.drawer.taskId); // fill any gap in the transcript
        else openFromHash();
        break;
      case 'project':
        if (change.isNew && !app.projectPinned) app.projectId = change.id; // "latest by default"
        invalidate('switcher', 'board', 'rail');
        break;
      case 'task': {
        const t = app.store.tasks.get(change.id);
        if (t.project_id === app.projectId) invalidate('board', 'switcher');
        else invalidate('switcher');
        if (app.drawer && app.drawer.taskId === change.id) invalidate('drawer');
        invalidate('fleet');
        break;
      }
      case 'event': {
        const t = app.store.tasks.get(change.id);
        if (t && t.assignee) bumpActivity(t.assignee.worker_id);
        if (t && t.project_id === app.projectId) invalidate('board');
        if (app.drawer && app.drawer.taskId === change.id) {
          app.drawer.events = L.mergeEvents(app.drawer.events, [change.item]);
          invalidate('drawer');
        }
        break;
      }
      case 'message':
        if (change.item.project_id === app.projectId) {
          if (!app.railOpen && isNarrow()) app.railUnread++;
          invalidate('rail');
          if (app.drawer) invalidate('drawer');
        }
        break;
      case 'worker':
        invalidate('fleet');
        break;
      default:
    }
  }

  /** Pick the current project: the user's choice if it still exists, else the newest. */
  function ensureProject() {
    const s = app.store;
    if (app.projectId && s.projects.has(app.projectId)) return;
    const latest = L.projectsByRecency(s)[0];
    const next = latest ? latest.id : null;
    if (next !== app.projectId) {
      app.projectId = next;
      app.projectPinned = false;
      dirty.board = dirty.rail = dirty.switcher = true;
    }
  }

  function selectProject(id, pinned = true) {
    if (id === app.projectId) return;
    app.projectId = id;
    app.projectPinned = pinned;
    if (pinned) sessionStorage.setItem('ov.project', id); else sessionStorage.removeItem('ov.project');
    resetBoardDom();
    app.railUnread = 0;
    invalidate('switcher', 'board', 'rail', 'fleet');
  }

  const currentProject = () => (app.projectId ? app.store.projects.get(app.projectId) : null);
  const isNarrow = () => window.matchMedia('(max-width: 1180px)').matches;

  // ================================================================== top bar: project switcher

  function renderSwitcher() {
    const p = currentProject();
    const projects = L.projectsByRecency(app.store);
    $('switcher').hidden = projects.length === 0;
    if (!p) return;
    const stats = L.projectStats(L.projectTasks(app.store, p.id));
    $('project-dot').className = `sdot ps-${p.status}`;
    $('project-title').textContent = p.title || p.prompt;
    $('project-meta').textContent = projectMeta(p, stats);
    $('project-btn').title = `${p.title}: ${p.status}`;

    const menu = $('project-menu');
    clear(menu);
    for (const q of projects) {
      const st = L.projectStats(L.projectTasks(app.store, q.id));
      const li = h('li', {
        role: 'option', tabindex: '-1', 'aria-selected': String(q.id === p.id), class: 'menu-item',
        data: { id: q.id },
        on: { click: () => { closeMenu(); selectProject(q.id); } },
      },
      h('span', { class: `sdot ps-${q.status}` }),
      h('span', { class: 'menu-title' }, q.title || q.prompt),
      h('span', { class: 'menu-meta mono' }, projectMeta(q, st)),
      h('span', { class: 'menu-time mono', data: { ts: q.created_at } }, L.relTime(q.created_at, now())));
      menu.appendChild(li);
    }
  }

  function projectMeta(p, stats) {
    if (p.status === 'planning') return 'planning';
    if (p.status === 'failed') return 'failed';
    return `${p.status} · ${stats.done}/${stats.total}`;
  }

  function openMenu() {
    const menu = $('project-menu');
    menu.hidden = false;
    $('project-btn').setAttribute('aria-expanded', 'true');
    const sel = menu.querySelector('[aria-selected="true"]') || menu.firstElementChild;
    if (sel) sel.focus();
  }
  function closeMenu() {
    $('project-menu').hidden = true;
    $('project-btn').setAttribute('aria-expanded', 'false');
  }
  $('project-btn').addEventListener('click', () => ($('project-menu').hidden ? openMenu() : closeMenu()));
  $('project-menu').addEventListener('keydown', (e) => {
    const items = [...$('project-menu').children];
    const i = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); (items[i + 1] || items[0]).focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); (items[i - 1] || items[items.length - 1]).focus(); }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (items[i]) items[i].click(); }
    else if (e.key === 'Escape') { e.stopPropagation(); closeMenu(); $('project-btn').focus(); }
  });
  document.addEventListener('click', (e) => { if (!$('switcher').contains(e.target)) closeMenu(); });

  // ================================================================== fleet strip

  const BUCKETS = 24; // x 2.5 s = last minute of transcript activity per laptop
  function bumpActivity(workerId) {
    if (!app.activity.has(workerId)) app.activity.set(workerId, new Array(BUCKETS).fill(0));
    app.activity.get(workerId)[BUCKETS - 1]++;
  }
  setInterval(() => {
    for (const arr of app.activity.values()) { arr.shift(); arr.push(0); }
    drawTraces();
  }, 2500);

  function renderFleet() {
    const wrap = $('fleet-chips');
    const workers = [...app.store.workers.values()];
    const order = { busy: 0, idle: 1, offline: 2 };
    workers.sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3) || a.id.localeCompare(b.id));
    clear(wrap);
    const online = workers.filter((w) => w.status !== 'offline').length;
    $('fleet').querySelector('.fleet-label').textContent = workers.length ? `Fleet ${online}/${workers.length}` : 'Fleet';
    if (!workers.length) {
      const target = app.mode === 'demo' ? '<hub>:8000' : (HUB || location.host || '<hub>:8000');
      wrap.appendChild(h('span', { class: 'fleet-empty' },
        'No workers yet. On each laptop run ',
        h('code', { class: 'mono' }, `cd worker && SERVER_URL=ws://${target}/worker uv run worker.py`)));
      return;
    }
    for (const w of workers) {
      const running = (w.running || []).map((id) => app.store.tasks.get(id)).filter(Boolean);
      const chip = h('div', {
        class: `wk ws-${w.status}`,
        title: `${w.laptop} (${w.owner})${w.tailscale_ip ? ' · ' + w.tailscale_ip : ''}${deviceText(w.device) ? '\n' + deviceText(w.device) : ''}\nagents: ${(w.agents || []).join(', ') || 'none'}` +
          `\n${w.status}${w.status === 'offline' ? ', last seen ' + L.relTime(w.last_seen, now()) + ' ago' : ''}`,
      },
      h('span', { class: 'wk-dot', 'aria-label': w.status }),
      h('span', { class: 'wk-name mono' }, w.laptop || w.id),
      h('span', { class: 'wk-owner' }, w.owner),
      deviceText(w.device) ? h('span', { class: 'wk-dev' }, deviceText(w.device)) : null,
      h('span', { class: 'wk-agents' }, (w.agents || []).map(agentBadge)),
      h('canvas', { class: 'trace', width: 48, height: 14, data: { worker: w.id }, 'aria-hidden': 'true' }),
      running.length
        ? running.map((t) => h('button', {
          class: 'wk-task mono', type: 'button', title: `${t.key}: ${t.title}`,
          on: { click: () => openTask(t.id) },
        }, t.key))
        : h('span', { class: 'wk-state' }, w.status === 'offline' ? 'offline' : 'idle'));
      wrap.appendChild(chip);
    }
    drawTraces();
  }

  function drawTraces() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#e0aa4f';
    const dim = getComputedStyle(document.documentElement).getPropertyValue('--line-2').trim() || '#303841';
    document.querySelectorAll('canvas.trace').forEach((cv) => {
      const w = 48, hgt = 14;
      if (cv.width !== w * dpr) { cv.width = w * dpr; cv.height = hgt * dpr; cv.style.width = w + 'px'; cv.style.height = hgt + 'px'; }
      const ctx = cv.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, hgt);
      const arr = app.activity.get(cv.dataset.worker) || [];
      const bw = w / BUCKETS;
      for (let i = 0; i < BUCKETS; i++) {
        const v = arr[i] || 0;
        const bh = v ? Math.max(2, Math.min(hgt, 2 + v * 3)) : 1;
        ctx.fillStyle = v ? accent : dim;
        ctx.fillRect(i * bw, hgt - bh, Math.max(1, bw - 1), bh);
      }
    });
  }

  // ================================================================== board

  const cardEls = new Map(); // task id -> {el, sig}
  let boardProject = null;

  function resetBoardDom() {
    cardEls.clear();
    boardProject = null;
    clear($('columns'));
  }

  function renderBoard() {
    const p = currentProject();
    const columns = $('columns');
    $('project-bar').hidden = !p;
    columns.hidden = !p;
    $('empty').hidden = !!p;
    $('planning').hidden = true;
    if (!p) { renderEmpty(); return; }

    const tasks = L.projectTasks(app.store, p.id);
    renderProjectBar(p, tasks);
    renderPlanning(p);

    // build the 5 columns once per project
    if (boardProject !== p.id || !columns.children.length) {
      resetBoardDom();
      boardProject = p.id;
      for (const c of L.COLUMNS) {
        columns.appendChild(h('section', { class: `col col-${c.id}`, data: { col: c.id }, 'aria-label': c.label },
          h('header', { class: 'col-head' },
            h('span', { class: 'col-mark', 'aria-hidden': 'true' }),
            h('h3', {}, c.label),
            h('span', { class: 'col-count mono' }, '0')),
          h('p', { class: 'col-empty' }),
          h('div', { class: 'col-list', role: 'list' })));
      }
    }

    // FLIP step 1: remember where every card is
    const animate = !REDUCED_MOTION.matches && !document.hidden;
    const first = new Map();
    if (animate) for (const [id, c] of cardEls) first.set(id, c.el.getBoundingClientRect());

    const byId = app.store.tasks;
    const seen = new Set();
    for (const c of L.COLUMNS) {
      const colEl = columns.querySelector(`[data-col="${c.id}"]`);
      const list = colEl.querySelector('.col-list');
      const items = L.sortColumn(c.id, tasks.filter((t) => L.columnOf(t) === c.id));
      colEl.querySelector('.col-count').textContent = String(items.length);
      const emptyEl = colEl.querySelector('.col-empty');
      emptyEl.textContent = items.length ? '' : emptyColumnHint(c.id, p);
      emptyEl.hidden = !!items.length;
      items.forEach((t, i) => {
        seen.add(t.id);
        const el = upsertCard(t, byId, first.size > 0 || cardEls.size > 0);
        // insertBefore moves the node out of its old column; cards bound for a later
        // column just trail behind this column's sorted range until that column claims them
        if (list.children[i] !== el) list.insertBefore(el, list.children[i] || null);
      });
    }
    for (const id of [...cardEls.keys()]) {
      if (!seen.has(id)) { cardEls.get(id).el.remove(); cardEls.delete(id); }
    }
    // safety net: drop anything left in a column it no longer belongs to
    for (const list of columns.querySelectorAll('.col-list')) {
      const colId = list.parentElement.dataset.col;
      [...list.children].forEach((el) => {
        const t = byId.get(el.dataset.id);
        if (!t || L.columnOf(t) !== colId) el.remove();
      });
    }

    // FLIP step 2: invert + play
    if (animate) {
      for (const [id, rect] of first) {
        const c = cardEls.get(id);
        if (!c || !c.el.isConnected) continue;
        const last = c.el.getBoundingClientRect();
        const dx = rect.left - last.left, dy = rect.top - last.top;
        if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
        c.el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'translate(0, 0)' }],
          { duration: Math.abs(dx) > 1 ? 320 : 220, easing: 'cubic-bezier(0.2, 0, 0, 1)' });
      }
    }
    if (app.drawer) markSelectedCard(app.drawer.taskId);
  }

  function emptyColumnHint(colId, p) {
    if (p.status === 'planning') return colId === 'todo' || colId === 'backlog' ? 'Tasks appear here once the plan lands.' : '';
    return { backlog: 'Nothing blocked.', todo: 'Nothing waiting.', progress: 'No agent running.', review: 'Nothing in review.', done: 'Nothing finished yet.' }[colId];
  }

  function cardSignature(t, byId) {
    return [t.status, t.title, t.progress, t.progress_note, t.last_event, t.pr_url, t.attempts, t.error,
      t.assignee && t.assignee.worker_id, t.assignee && t.assignee.agent, t.started_at, t.finished_at, t.priority,
      L.pendingDeps(t, byId).join(',')].join('\u0001');
  }

  function upsertCard(t, byId, flashOnChange) {
    const sig = cardSignature(t, byId);
    let c = cardEls.get(t.id);
    if (c && c.sig === sig) return c.el;
    const isNew = !c;
    if (!c) {
      const el = h('article', {
        class: 'card', tabindex: '0', role: 'listitem', data: { id: t.id },
        on: {
          click: (e) => { if (!e.target.closest('a')) openTask(t.id); },
          keydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openTask(t.id); } },
        },
      });
      c = { el, sig: '' };
      cardEls.set(t.id, c);
    }
    const prevStatus = c.el.dataset.status;
    c.sig = sig;
    fillCard(c.el, t, byId);
    if (isNew && flashOnChange && !REDUCED_MOTION.matches) {
      c.el.classList.add('is-new');
      setTimeout(() => c.el.classList.remove('is-new'), 400);
    } else if (!isNew) {
      c.el.classList.remove('is-updated');
      void c.el.offsetWidth; // restart the highlight transition
      c.el.classList.add('is-updated');
      clearTimeout(c.flashTimer);
      c.flashTimer = setTimeout(() => c.el.classList.remove('is-updated'), prevStatus !== t.status ? 1200 : 700);
    }
    return c.el;
  }

  function fillCard(el, t, byId) {
    el.dataset.status = t.status;
    el.className = `card st-${t.status}${t.kind === 'plan' ? ' is-plan' : ''}${el.classList.contains('is-selected') ? ' is-selected' : ''}`;
    el.setAttribute('aria-label', `${t.key}: ${t.title}, ${L.STATUS_LABEL[t.status] || t.status}`);
    const active = L.ACTIVE_STATUSES.has(t.status);
    const note = t.status === 'failed' ? t.error : (t.progress_note || t.last_event);
    const pending = t.status === 'backlog' ? L.pendingDeps(t, byId) : [];
    const showTime = active || L.TERMINAL_STATUSES.has(t.status);
    const elapsed = showTime ? L.taskElapsed(t, now()) : null;

    const badges = [];
    if (t.status === 'failed') badges.push(h('span', { class: 'badge b-err' }, 'failed'));
    if (t.status === 'cancelled') badges.push(h('span', { class: 'badge' }, 'cancelled'));
    if (t.attempts > 1) badges.push(h('span', { class: 'badge b-warn mono', title: `${t.attempts} attempts` }, `try ${t.attempts}`));
    if (t.error && t.status !== 'failed') badges.push(h('span', { class: 'badge b-err', title: t.error }, 'error'));
    if (t.pr_url) {
      badges.push(h('a', { class: 'badge b-pr mono', href: t.pr_url, target: '_blank', rel: 'noopener noreferrer', title: t.pr_url },
        L.prNumber(t.pr_url) ? `PR #${L.prNumber(t.pr_url)}` : 'PR'));
    }

    clear(el);
    append(el, [
      h('div', { class: 'card-top' },
        h('span', { class: 'key mono' }, t.key),
        t.priority === 1 ? h('span', { class: 'prio mono', title: 'Priority 1 (urgent)' }, 'P1') : null,
        h('span', { class: 'grow' }),
        elapsed != null ? h('span', {
          class: 'elapsed mono', title: active ? 'running for' : 'ran for',
          data: { start: t.started_at, end: active ? '' : (t.finished_at || t.updated_at || '') },
        }, L.fmtDuration(elapsed)) : null),
      h('div', { class: 'card-title' }, t.title),
      note && t.status !== 'backlog' && t.status !== 'done' ? h('div', { class: 'card-note', title: note }, L.firstLine(note, 200)) : null,
      pending.length ? h('div', { class: 'card-deps mono' }, `needs ${pending.join(', ')}`) : null,
      active || (t.progress > 0 && t.status === 'todo') ? h('div', { class: 'card-progress' },
        h('div', { class: 'meter', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(t.progress || 0) },
          h('i', { style: `width:${Math.max(0, Math.min(100, t.progress || 0))}%` })),
        h('span', { class: 'pct mono' }, `${Math.round(t.progress || 0)}%`)) : null,
      (t.assignee || badges.length) ? h('div', { class: 'card-foot' },
        t.assignee ? h('span', { class: 'who', title: `${t.assignee.agent} on ${t.assignee.laptop} (${t.assignee.owner})` },
          agentBadge(t.assignee.agent), h('span', { class: 'mono' }, t.assignee.laptop)) : null,
        h('span', { class: 'grow' }),
        badges) : null,
    ]);
  }

  function markSelectedCard(id) {
    for (const [tid, c] of cardEls) c.el.classList.toggle('is-selected', tid === id);
  }

  // ---------------------------------------------------------------- project bar + planning strip

  function renderProjectBar(p, tasks) {
    const bar = $('project-bar');
    const stats = L.projectStats(tasks);
    clear(bar);
    append(bar, [
      h('div', { class: 'pb-main' },
        h('h1', { class: 'pb-title', title: p.prompt }, p.title || p.prompt),
        h('span', { class: `pill ps-${p.status}` }, p.status),
        h('span', { class: 'pb-repo mono', title: p.repo ? `GitHub repo, base branch ${p.base_branch}` : 'Local mode: no push, PR or merge' },
          p.repo ? `${p.repo} @ ${p.base_branch}` : 'local mode'),
        stats.total ? h('span', { class: 'pb-stats mono' },
          `${stats.done}/${stats.total} done${stats.failed ? ` · ${stats.failed} failed` : ''}`) : null,
        h('span', { class: 'grow' }),
        h('button', { class: 'btn ghost', type: 'button', disabled: p.status === 'planning' ? true : null,
          title: p.status === 'planning' ? 'Available once the plan lands' : null,
          on: { click: () => openAddTask(p) } }, 'Add task')),
      h('p', { class: 'pb-summary', title: p.summary || p.prompt }, p.summary || p.prompt),
    ]);
  }

  function renderPlanning(p) {
    const el = $('planning');
    const plan = app.store.tasks.get(p.plan_task_id);
    if (!plan || (p.status !== 'planning' && p.status !== 'failed')) { el.hidden = true; return; }
    el.hidden = false;
    clear(el);
    el.className = `planning ${p.status === 'failed' ? 'is-failed' : ''}`;
    const online = [...app.store.workers.values()].filter((w) => w.status !== 'offline').length;
    let line;
    if (p.status === 'failed') line = plan.error || 'The planner gave up.';
    else if (plan.status === 'todo') line = online ? 'Waiting for a free worker to pick up the plan.' : 'No worker is online. Start one on any laptop to begin planning.';
    else line = plan.last_event || plan.progress_note || 'Starting the planner…';
    append(el, [
      h('span', { class: 'pl-label' }, p.status === 'failed' ? 'Planning failed' : 'Planning…'),
      plan.assignee ? h('span', { class: 'who' }, agentBadge(plan.assignee.agent),
        h('span', { class: 'mono' }, `${plan.assignee.agent}@${plan.assignee.laptop}`)) : null,
      h('button', { class: 'pl-line', type: 'button', title: 'Open the planner transcript', on: { click: () => openTask(plan.id) } }, line),
      plan.status === 'in_progress' ? h('span', { class: 'pct mono' }, `${plan.progress || 0}%`) : null,
      plan.started_at ? h('span', { class: 'elapsed mono', data: { start: plan.started_at, end: plan.finished_at || '' } },
        L.fmtDuration(L.taskElapsed(plan, now()))) : null,
      p.status === 'failed' ? h('button', { class: 'btn', type: 'button', on: { click: () => taskAction(plan, 'retry') } }, 'Retry planning') : null,
    ]);
  }

  // ---------------------------------------------------------------- empty state (composer)

  function renderEmpty() {
    const el = $('empty');
    if (el.firstChild) return; // keep whatever the user is typing

    append(el, [
      h('div', { class: 'empty-inner' },
        h('h1', { class: 'empty-title' }, 'Give the fleet something to build'),
        h('p', { class: 'empty-sub' },
          'Describe a product. One agent splits it into tasks, then every connected laptop picks them up in parallel.'),
        buildComposer(() => {})),
    ]);
  }

  // ================================================================== composer (new product)

  function buildComposer(onDone) {
    const prompt = h('textarea', { id: uid('prompt'), name: 'prompt', rows: '6', required: true,
      placeholder: 'A pomodoro web app with session history and a weekly stats chart. Keyboard driven, works offline.' });
    const repo = h('input', { id: uid('repo'), name: 'repo', placeholder: 'owner/name', spellcheck: 'false', autocapitalize: 'off',
      pattern: '[\\w.\\-]+/[\\w.\\-]+' });
    const base = h('input', { id: uid('base'), name: 'base', value: 'main', spellcheck: 'false', autocapitalize: 'off' });
    const submit = h('button', { class: 'btn primary', type: 'submit' }, 'Launch');
    const err = h('p', { class: 'form-err', role: 'alert', hidden: true });

    const form = h('form', { class: 'composer', autocomplete: 'off', novalidate: true },
      h('label', { class: 'composer-label', for: prompt.id }, 'What should the agents build?'),
      prompt,
      h('div', { class: 'composer-row' },
        h('div', { class: 'field grow' },
          h('label', { for: repo.id }, 'GitHub repo'),
          repo,
          h('p', { class: 'hint' }, 'Optional. Agents push branches and merge PRs here. Leave it empty for local mode: work stays in a git workspace on each laptop, with no pushes or PRs.')),
        h('div', { class: 'field base' },
          h('label', { for: base.id }, 'Base branch'),
          base)),
      err,
      h('div', { class: 'composer-actions' },
        h('span', { class: 'hint mono' }, navigator.platform.includes('Mac') ? '⌘ Enter' : 'Ctrl Enter'),
        submit));

    prompt.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); form.requestSubmit(); }
    });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      err.hidden = true;
      const body = { prompt: prompt.value.trim(), repo: repo.value.trim(), base_branch: base.value.trim() || 'main' };
      if (!body.prompt) { showErr('Describe what to build first.'); prompt.focus(); return; }
      if (body.repo && !/^[\w.-]+\/[\w.-]+$/.test(body.repo)) { showErr('Repo must look like owner/name.'); repo.focus(); return; }
      submit.disabled = true;
      submit.textContent = 'Launching…';
      try {
        const p = await api('POST', '/api/projects', body);
        form.reset();
        base.value = 'main';
        onDone(p);
        if (p && p.id) {
          app.projectPinned = false;
          sessionStorage.removeItem('ov.project');
          // the project frame may arrive before or after this response
          if (!app.store.projects.has(p.id)) L.applyFrame(app.store, { type: 'project', project: p });
          selectProject(p.id, false);
        }
        toast('Launched. Planning has started.');
      } catch (ex) {
        showErr(`Launch failed: ${ex.message}`);
      } finally {
        submit.disabled = false;
        submit.textContent = 'Launch';
      }
    });
    function showErr(msg) { err.textContent = msg; err.hidden = false; }
    return form;
  }

  let uidSeq = 0;
  const uid = (p) => `${p}-${++uidSeq}`;

  function openComposerDialog() {
    const dlg = $('composer-dialog');
    clear(dlg);
    append(dlg, [
      h('header', { class: 'dialog-head' },
        h('h2', {}, 'New product'),
        h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Close', on: { click: () => dlg.close() } }, closeIcon())),
      buildComposer(() => dlg.close()),
    ]);
    dlg.showModal();
    dlg.querySelector('textarea').focus();
  }
  $('new-product').addEventListener('click', openComposerDialog);

  function closeIcon() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 12 12');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M2 2l8 8M10 2l-8 8');
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '1.4');
    svg.appendChild(path);
    return svg;
  }

  // ================================================================== add task dialog

  function openAddTask(p) {
    const dlg = $('addtask-dialog');
    const tasks = L.sortColumn('todo', L.projectTasks(app.store, p.id).filter((t) => t.kind === 'build'));
    const title = h('input', { id: uid('t'), required: true, placeholder: 'e.g. Dark mode toggle' });
    const desc = h('textarea', { id: uid('d'), rows: '4', placeholder: 'What to build, files or areas, interfaces to honour. Markdown is fine.' });
    const acc = h('textarea', { id: uid('a'), rows: '3', placeholder: 'One check per line' });
    const prio = h('select', { id: uid('p') },
      [1, 2, 3, 4].map((n) => h('option', { value: String(n), selected: n === 2 ? true : null },
        `P${n}${n === 1 ? ' · urgent' : n === 4 ? ' · low' : ''}`)));
    const agent = h('select', { id: uid('g') },
      h('option', { value: '' }, 'Any agent'),
      ['claude-code', 'codex', 'vibe', 'cursor', 'mock'].map((a) => h('option', { value: a }, a)));
    const deps = h('div', { class: 'dep-picks' }, tasks.length ? tasks.map((t) => h('label', { class: 'dep-pick' },
      h('input', { type: 'checkbox', value: t.id }),
      h('span', { class: 'mono' }, t.key), ' ', h('span', { class: 'dim' }, t.title))) : h('p', { class: 'hint' }, 'No tasks yet.'));
    const err = h('p', { class: 'form-err', role: 'alert', hidden: true });
    const submit = h('button', { class: 'btn primary', type: 'submit' }, 'Add task');

    const form = h('form', { class: 'task-form', autocomplete: 'off' },
      h('div', { class: 'field' }, h('label', { for: title.id }, 'Title'), title),
      h('div', { class: 'field' }, h('label', { for: desc.id }, 'Description'), desc),
      h('div', { class: 'field' }, h('label', { for: acc.id }, 'Acceptance checks'), acc),
      h('div', { class: 'composer-row' },
        h('div', { class: 'field' }, h('label', { for: prio.id }, 'Priority'), prio),
        h('div', { class: 'field' }, h('label', { for: agent.id }, 'Preferred agent'), agent)),
      h('fieldset', { class: 'field' }, h('legend', {}, 'Depends on'), deps),
      err,
      h('div', { class: 'composer-actions' }, h('span', { class: 'grow' }), submit));

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!title.value.trim()) { title.focus(); return; }
      submit.disabled = true;
      try {
        const t = await api('POST', `/api/projects/${encodeURIComponent(p.id)}/tasks`, {
          title: title.value.trim(),
          description: desc.value.trim(),
          acceptance: acc.value.split('\n').map((s) => s.trim()).filter(Boolean),
          depends_on: [...deps.querySelectorAll('input:checked')].map((i) => i.value),
          priority: Number(prio.value),
          agent_hint: agent.value,
        });
        dlg.close();
        toast(`Added ${t && t.key ? t.key : 'task'}.`);
      } catch (ex) {
        err.textContent = `Could not add the task: ${ex.message}`;
        err.hidden = false;
      } finally { submit.disabled = false; }
    });

    clear(dlg);
    append(dlg, [
      h('header', { class: 'dialog-head' },
        h('h2', {}, `Add task to ${p.title}`),
        h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Close', on: { click: () => dlg.close() } }, closeIcon())),
      form,
    ]);
    dlg.showModal();
    title.focus();
  }

  // ================================================================== chatter rail

  let railRenderedFor = null;
  let railCount = 0;

  function projectMessages() {
    return app.store.messages.filter((m) => m.project_id === app.projectId);
  }

  function messageItem(m, compact) {
    const from = m.from || {};
    const fromTask = from.task_id ? app.store.tasks.get(from.task_id) : null;
    const toTask = app.store.tasks.get(m.to);
    const reply = m.reply_to ? app.store.messages.find((x) => x.id === m.reply_to) : null;
    return h('li', { class: `msg k-${from.kind || 'system'}`, data: { id: m.id } },
      h('div', { class: 'msg-head' },
        from.kind === 'agent' ? agentBadge(from.agent) : h('span', { class: `ag ag-${from.kind === 'human' ? 'human' : 'sys'}`, 'aria-hidden': 'true' },
          from.kind === 'human' ? (from.name || 'h').charAt(0).toUpperCase() : '·'),
        fromTask
          ? h('button', { class: 'msg-from linkish', type: 'button', on: { click: () => openTask(fromTask.id) } }, from.name || fromTask.key)
          : h('span', { class: 'msg-from' }, from.name || from.kind || 'system'),
        compact && m.to === 'all' ? null : h('span', { class: 'msg-arrow', 'aria-label': 'to' }, '→'),
        compact && m.to === 'all' ? null : (toTask
          ? h('button', { class: 'msg-to mono linkish', type: 'button', on: { click: () => openTask(toTask.id) } }, toTask.key)
          : h('span', { class: 'msg-to' }, L.recipientLabel(m.to, app.store))),
        h('span', { class: 'grow' }),
        h('time', { class: 'mono rel', data: { ts: m.ts }, title: new Date(m.ts * 1000).toLocaleString() }, L.relTime(m.ts, now()))),
      reply ? h('div', { class: 'msg-reply' }, `↳ ${(reply.from && reply.from.name) || 'message'}: ${L.firstLine(reply.text, 80)}`) : null,
      h('div', { class: 'msg-text' }, m.text));
  }

  function renderRail() {
    const list = $('rail-msgs');
    const msgs = projectMessages();
    const stick = list.scrollTop + list.clientHeight >= list.scrollHeight - 24;
    if (railRenderedFor !== app.projectId || msgs.length < railCount) {
      clear(list);
      railCount = 0;
      railRenderedFor = app.projectId;
    }
    for (let i = railCount; i < msgs.length; i++) list.appendChild(messageItem(msgs[i], false));
    railCount = msgs.length;
    if (!msgs.length) {
      if (!list.firstChild) list.appendChild(h('li', { class: 'msg-empty' }, app.projectId
        ? 'Agents post here with `ov say`. You can message everyone or a single task below.'
        : 'Messages between agents and people show up here.'));
    } else {
      const empty = list.querySelector('.msg-empty');
      if (empty) empty.remove();
    }
    $('rail-count').textContent = msgs.length ? String(msgs.length) : '';
    if (stick) list.scrollTop = list.scrollHeight;
    renderSayForm($('rail-form'), null);
    updateRailToggle();
  }

  /**
   * The "message someone" form, used in the rail (taskId=null, recipient picker) and in the
   * drawer's Chat tab (fixed recipient = the task). Built once; later calls only refresh the
   * recipient options, so typing and focus survive live updates.
   */
  function renderSayForm(form, taskId) {
    const p = currentProject();
    form.hidden = !p;
    if (!p) return;
    if (!form.dataset.built) buildSayForm(form, taskId);
    if (taskId) return;
    const tasks = L.sortColumn('backlog', L.projectTasks(app.store, p.id));
    const sig = [p.id, ...tasks.map((t) => t.id), ...app.store.workers.keys()].join('|');
    if (form.dataset.sig === sig) return;
    form.dataset.sig = sig;
    const to = form.querySelector('select');
    const keep = to.value || 'all';
    clear(to);
    append(to, [
      h('option', { value: 'all' }, 'Everyone'),
      tasks.length ? h('optgroup', { label: 'Tasks' }, tasks.map((t) => h('option', { value: t.id }, `${t.key} · ${L.firstLine(t.title, 40)}`))) : null,
      app.store.workers.size ? h('optgroup', { label: 'Laptops' }, [...app.store.workers.values()].map((w) => h('option', { value: w.id }, w.laptop || w.id))) : null,
    ]);
    to.value = [...to.options].some((o) => o.value === keep) ? keep : 'all';
  }

  function buildSayForm(form, taskId) {
    form.dataset.built = '1';
    const to = taskId ? h('input', { type: 'hidden', name: 'to', value: taskId })
      : h('select', { name: 'to', 'aria-label': 'Recipient' }, h('option', { value: 'all' }, 'Everyone'));
    const ta = h('textarea', { name: 'text', rows: '2', placeholder: taskId ? 'Message this task’s agent. Enter sends, Shift Enter adds a line.' : 'Message the agents. Enter sends, Shift Enter adds a line.', 'aria-label': 'Message' });
    const name = h('input', { name: 'name', class: 'say-name', placeholder: 'your name', value: app.name, 'aria-label': 'Your name', spellcheck: 'false' });
    const send = h('button', { class: 'btn', type: 'submit' }, 'Send');
    append(form, [
      taskId ? to : h('div', { class: 'say-to' }, h('span', { class: 'dim' }, 'To'), to),
      ta,
      h('div', { class: 'say-row' }, h('span', { class: 'dim' }, 'as'), name, h('span', { class: 'grow' }), send),
    ]);
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
    });
    name.addEventListener('change', () => {
      app.name = name.value.trim();
      localStorage.setItem('ov.name', app.name);
      document.querySelectorAll('.say-name').forEach((el) => { if (el !== name) el.value = app.name; });
    });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const p = currentProject();
      if (!p) return;
      const body = { project_id: taskId ? (app.store.tasks.get(taskId) || p).project_id || p.id : p.id, to: to.value || 'all', text: ta.value.trim() };
      if (!body.text) return;
      if (app.name) body.name = app.name;
      send.disabled = true;
      try {
        const m = await api('POST', '/api/messages', body);
        ta.value = '';
        if (m && m.id && L.addMessage(app.store, m)) invalidate('rail', 'drawer');
      } catch (ex) {
        toast(`Message not sent: ${ex.message}`, 'error');
      } finally { send.disabled = false; ta.focus(); }
    });
  }

  function updateRailToggle() {
    const n = $('rail-unread');
    n.hidden = !app.railUnread;
    n.textContent = String(app.railUnread);
  }
  $('rail-toggle').addEventListener('click', () => {
    app.railOpen = !app.railOpen;
    document.body.classList.toggle('rail-open', app.railOpen);
    $('rail-toggle').setAttribute('aria-expanded', String(app.railOpen));
    if (app.railOpen) { app.railUnread = 0; updateRailToggle(); const l = $('rail-msgs'); l.scrollTop = l.scrollHeight; }
  });

  // ================================================================== task drawer

  function openTask(taskId, { fromHash = false } = {}) {
    const t = app.store.tasks.get(taskId);
    if (t && t.project_id !== app.projectId) selectProject(t.project_id);
    if (!app.drawer || app.drawer.taskId !== taskId) {
      app.drawer = { taskId, events: [], renderedSeq: 0, tab: 'transcript', loading: true, stick: true, unseen: 0, built: false };
      loadDrawerTask(taskId);
    }
    if (!fromHash) history.replaceState(null, '', `#task=${encodeURIComponent(taskId)}`);
    $('drawer').hidden = false;
    document.body.classList.add('drawer-open');
    markSelectedCard(taskId);
    invalidate('drawer');
  }

  function closeDrawer() {
    if (!app.drawer) return;
    const id = app.drawer.taskId;
    app.drawer = null;
    $('drawer').hidden = true;
    clear($('drawer'));
    document.body.classList.remove('drawer-open');
    markSelectedCard(null);
    if (location.hash) history.replaceState(null, '', location.pathname + location.search);
    const card = cardEls.get(id);
    if (card) card.el.focus({ preventScroll: true });
  }

  async function loadDrawerTask(taskId) {
    try {
      const res = await api('GET', `/api/tasks/${encodeURIComponent(taskId)}`);
      if (!app.drawer || app.drawer.taskId !== taskId) return;
      if (res.task) {
        const known = app.store.tasks.get(taskId);
        if (!known || (res.task.updated_at || 0) >= (known.updated_at || 0)) app.store.tasks.set(taskId, res.task);
        if (res.task.project_id !== app.projectId && app.store.projects.has(res.task.project_id)) selectProject(res.task.project_id);
      }
      app.drawer.events = L.mergeEvents(app.drawer.events, res.events || []);
      app.drawer.loading = false;
      app.drawer.error = '';
      invalidate('board');
    } catch (ex) {
      if (!app.drawer || app.drawer.taskId !== taskId) return;
      app.drawer.loading = false;
      app.drawer.error = ex.status === 404 ? 'This task does not exist on the hub.' : `Could not load the transcript: ${ex.message}`;
    }
    invalidate('drawer');
  }

  function openFromHash() {
    const m = /^#task=(.+)$/.exec(location.hash);
    if (m) {
      const id = decodeURIComponent(m[1]);
      if (!app.drawer || app.drawer.taskId !== id) openTask(id, { fromHash: true });
    } else if (app.drawer) closeDrawer();
  }
  window.addEventListener('hashchange', openFromHash);

  function renderDrawer() {
    const d = app.drawer;
    const root = $('drawer');
    if (!d) return;
    const t = app.store.tasks.get(d.taskId);
    if (!t) {
      clear(root);
      d.built = false;
      append(root, [h('header', { class: 'd-head' },
        h('div', { class: 'd-row' }, h('span', { class: 'key mono' }, d.taskId), h('span', { class: 'grow' }),
          h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Close', on: { click: closeDrawer } }, closeIcon()))),
      h('p', { class: 'd-msg' }, d.loading ? 'Loading…' : (d.error || 'Task not found.'))]);
      return;
    }
    if (!d.built) buildDrawerSkeleton(root, d);
    fillDrawerHead(root, t);
    fillBrief(root, t);
    const counts = { transcript: d.events.length, chat: chatFor(t).length };
    root.querySelectorAll('.tab').forEach((b) => {
      const n = b.querySelector('.tab-n');
      if (n) n.textContent = counts[b.dataset.tab] ? String(counts[b.dataset.tab]) : '';
      b.setAttribute('aria-selected', String(b.dataset.tab === d.tab));
      b.tabIndex = b.dataset.tab === d.tab ? 0 : -1;
    });
    root.querySelectorAll('.d-panel').forEach((pnl) => { pnl.hidden = pnl.dataset.tab !== d.tab; });
    renderTranscript(root.querySelector('.d-panel[data-tab="transcript"]'), d, t);
    renderTaskChat(root.querySelector('.d-panel[data-tab="chat"]'), t);
    renderResult(root.querySelector('.d-panel[data-tab="result"]'), t);
  }

  function buildDrawerSkeleton(root, d) {
    clear(root);
    d.built = true;
    d.renderedSeq = 0;
    const tabs = [['transcript', 'Transcript'], ['chat', 'Chat'], ['result', 'Result']];
    const tablist = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Task detail' },
      tabs.map(([id, label]) => h('button', {
        class: 'tab', type: 'button', role: 'tab', id: `tab-${id}`, 'aria-controls': `panel-${id}`, data: { tab: id },
        on: { click: () => { d.tab = id; renderDrawer(); if (id === 'transcript') scrollTranscriptToEnd(true); } },
      }, label, h('span', { class: 'tab-n mono' }))));
    tablist.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const ids = tabs.map((x) => x[0]);
      const i = ids.indexOf(d.tab);
      d.tab = ids[(i + (e.key === 'ArrowRight' ? 1 : ids.length - 1)) % ids.length];
      renderDrawer();
      root.querySelector(`#tab-${d.tab}`).focus();
    });
    const transcript = h('div', { class: 'd-panel transcript', role: 'tabpanel', id: 'panel-transcript', 'aria-labelledby': 'tab-transcript', data: { tab: 'transcript' } },
      h('div', { class: 'tx-list', role: 'log', 'aria-live': 'off' }),
      h('button', { class: 'tx-jump', type: 'button', hidden: true, on: { click: () => scrollTranscriptToEnd(true) } }));
    transcript.addEventListener('scroll', () => {
      const atEnd = transcript.scrollTop + transcript.clientHeight >= transcript.scrollHeight - 32;
      d.stick = atEnd;
      if (atEnd) { d.unseen = 0; updateJump(transcript, d); }
    }, { passive: true });
    append(root, [
      h('header', { class: 'd-head' }),
      h('details', { class: 'd-brief', open: true },
        h('summary', {}, 'Brief'),
        h('div', { class: 'd-brief-body' })),
      tablist,
      transcript,
      h('div', { class: 'd-panel chat', role: 'tabpanel', id: 'panel-chat', 'aria-labelledby': 'tab-chat', data: { tab: 'chat' } },
        h('ol', { class: 'msgs' }), h('form', { class: 'say', autocomplete: 'off' })),
      h('div', { class: 'd-panel result', role: 'tabpanel', id: 'panel-result', 'aria-labelledby': 'tab-result', data: { tab: 'result' } }),
    ]);
  }

  function fillDrawerHead(root, t) {
    const head = root.querySelector('.d-head');
    const sig = [t.status, t.title, t.progress, t.progress_note, t.pr_url, t.branch, t.attempts, t.error,
      JSON.stringify(t.assignee), JSON.stringify(t.tokens), t.started_at, t.finished_at].join('\u0001');
    if (head.dataset.sig === sig) return;
    head.dataset.sig = sig;
    const active = L.ACTIVE_STATUSES.has(t.status);
    const elapsed = L.taskElapsed(t, now());
    const actions = [];
    if (['failed', 'cancelled', 'todo'].includes(t.status)) actions.push(h('button', { class: 'btn', type: 'button', on: { click: () => taskAction(t, 'retry') } }, 'Retry'));
    if (t.status === 'review') actions.push(h('button', { class: 'btn primary', type: 'button', on: { click: () => taskAction(t, 'done') } }, 'Mark done'));
    if (!L.TERMINAL_STATUSES.has(t.status)) {
      const cancel = h('button', { class: 'btn danger', type: 'button' }, 'Cancel');
      cancel.addEventListener('click', () => {
        if (cancel.dataset.armed) { taskAction(t, 'cancel'); return; }
        cancel.dataset.armed = '1';
        cancel.textContent = 'Confirm cancel';
        setTimeout(() => { if (cancel.isConnected) { delete cancel.dataset.armed; cancel.textContent = 'Cancel'; } }, 3000);
      });
      actions.push(cancel);
    }
    const meta = [];
    const row = (k, ...v) => meta.push(h('div', { class: 'kv' }, h('dt', {}, k), h('dd', {}, ...v)));
    row('Assignee', t.assignee
      ? h('span', { class: 'who' }, agentBadge(t.assignee.agent), h('span', { class: 'mono' }, `${t.assignee.agent}@${t.assignee.laptop}`), h('span', { class: 'dim' }, ` ${t.assignee.owner}`))
      : h('span', { class: 'dim' }, 'unassigned'));
    row('Branch', t.branch ? h('span', { class: 'mono copyable', title: 'Click to copy', on: { click: () => copy(t.branch) } }, t.branch) : h('span', { class: 'dim' }, '—'));
    row('PR', t.pr_url ? h('a', { class: 'mono', href: t.pr_url, target: '_blank', rel: 'noopener noreferrer' },
      L.prNumber(t.pr_url) ? `#${L.prNumber(t.pr_url)}` : t.pr_url) : h('span', { class: 'dim' }, '—'));
    row('Tokens', h('span', { class: 'mono' }, `${fmtNum(t.tokens && t.tokens.input)} in · ${fmtNum(t.tokens && t.tokens.output)} out`));
    row('Attempts', h('span', { class: 'mono' }, String(t.attempts || 0)));
    row('Time', elapsed != null
      ? h('span', { class: 'mono' }, h('span', { class: 'elapsed', data: { start: t.started_at, end: active ? '' : (t.finished_at || t.updated_at || '') } }, L.fmtDuration(elapsed)),
        h('span', { class: 'dim' }, ` · ${L.clockTime(t.started_at)}${t.finished_at ? `–${L.clockTime(t.finished_at)}` : ''}`))
      : h('span', { class: 'dim' }, 'not started'));

    clear(head);
    append(head, [
      h('div', { class: 'd-row' },
        h('span', { class: 'key mono' }, t.key),
        statusPill(t.status),
        t.priority ? h('span', { class: 'mono dim', title: 'priority' }, `P${t.priority}`) : null,
        h('span', { class: 'grow' }),
        h('div', { class: 'd-actions' }, actions),
        h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Close (Esc)', title: 'Close (Esc)', on: { click: closeDrawer } }, closeIcon())),
      h('h2', { id: 'd-title', class: 'd-title' }, t.title),
      active || t.progress ? h('div', { class: 'card-progress d-progress' },
        h('div', { class: 'meter' }, h('i', { style: `width:${Math.max(0, Math.min(100, t.progress || 0))}%` })),
        h('span', { class: 'pct mono' }, `${Math.round(t.progress || 0)}%`),
        h('span', { class: 'd-note', title: t.progress_note || '' }, t.progress_note || '')) : null,
      t.error ? h('p', { class: 'd-error' }, t.error) : null,
      h('dl', { class: 'd-meta' }, meta),
    ]);
  }

  function fillBrief(root, t) {
    const body = root.querySelector('.d-brief-body');
    const byId = app.store.tasks;
    const blocks = [...byId.values()].filter((x) => (x.depends_on || []).includes(t.id));
    const sig = [t.description, (t.acceptance || []).join('\u0002'), t.status,
      (t.depends_on || []).map((id) => id + ((byId.get(id) || {}).status || '')).join(','),
      blocks.map((x) => x.id + x.status).join(',')].join('\u0001');
    if (body.dataset.sig === sig) return;
    body.dataset.sig = sig;
    clear(body);
    const depChip = (id) => {
      const d = byId.get(id);
      return h('button', { class: `dep st-${d ? d.status : 'todo'}`, type: 'button', title: d ? `${d.title} (${L.STATUS_LABEL[d.status]})` : id,
        on: { click: () => openTask(id) } }, h('span', { class: 'sdot' }), h('span', { class: 'mono' }, d ? d.key : id));
    };
    append(body, [
      t.description ? setMarkdown(h('div', { class: 'md' }), t.description) : h('p', { class: 'dim' }, 'No description.'),
      (t.acceptance || []).length ? h('div', { class: 'acc' },
        h('h4', {}, 'Acceptance'),
        h('ul', { class: 'checks' }, t.acceptance.map((a) => h('li', { class: t.status === 'done' ? 'is-done' : '' },
          h('span', { class: 'check', 'aria-hidden': 'true' }), a)))) : null,
      (t.depends_on || []).length || blocks.length ? h('div', { class: 'deps-row' },
        (t.depends_on || []).length ? [h('span', { class: 'dim' }, 'Depends on'), t.depends_on.map(depChip)] : null,
        blocks.length ? [h('span', { class: 'dim' }, 'Unblocks'), blocks.map((x) => depChip(x.id))] : null) : null,
    ]);
  }

  // ---------------------------------------------------------------- transcript

  function renderTranscript(panel, d, t) {
    const list = panel.querySelector('.tx-list');
    if (d.loading && !d.events.length) {
      if (!list.firstChild) list.appendChild(h('p', { class: 'd-msg' }, 'Loading transcript…'));
      return;
    }
    if (d.error && !d.events.length) { clear(list); list.appendChild(h('p', { class: 'd-msg err' }, d.error)); return; }
    const evs = d.events;
    // a gap-filling fetch can deliver older seqs: re-render from scratch in that case
    const maxRendered = d.renderedSeq;
    const needsFull = list.dataset.count && evs.filter((e) => e.seq <= maxRendered).length !== Number(list.dataset.count);
    if (needsFull || !evs.length) { clear(list); d.renderedSeq = 0; list.dataset.count = '0'; }
    if (!evs.length) {
      list.appendChild(h('p', { class: 'd-msg' }, t.status === 'backlog' || t.status === 'todo'
        ? 'No transcript yet. It streams here once an agent picks the task up.' : 'No events recorded.'));
      return;
    }
    const empty = list.querySelector('.d-msg');
    if (empty) empty.remove();
    let added = 0;
    for (const e of evs) {
      if (e.seq <= d.renderedSeq) continue;
      list.appendChild(eventNode(e));
      d.renderedSeq = e.seq;
      added++;
    }
    list.dataset.count = String(evs.filter((e) => e.seq <= d.renderedSeq).length);
    if (!added) return;
    if (d.stick || panel.hidden) scrollTranscriptToEnd(false);
    else { d.unseen += added; updateJump(panel, d); }
  }

  function scrollTranscriptToEnd(force) {
    const d = app.drawer;
    const panel = document.querySelector('#drawer .d-panel.transcript');
    if (!d || !panel) return;
    if (force) d.stick = true;
    panel.scrollTop = panel.scrollHeight;
    d.unseen = 0;
    updateJump(panel, d);
  }

  function updateJump(panel, d) {
    const b = panel.querySelector('.tx-jump');
    b.hidden = !d.unseen;
    b.textContent = d.unseen === 1 ? '1 new event ↓' : `${d.unseen} new events ↓`;
  }

  function eventNode(e) {
    const kind = e.kind || 'system';
    const time = h('time', { class: 'ev-time mono', title: new Date(e.ts * 1000).toLocaleString() }, L.clockTime(e.ts));
    const text = String(e.text || '');
    const lines = text.split('\n');
    const short = lines.length <= 1 && text.length <= 140;
    let body;
    switch (kind) {
      case 'assistant':
        body = setMarkdown(h('div', { class: 'md' }), text);
        break;
      case 'thinking':
        body = short ? h('div', { class: 'ev-think' }, text)
          : h('details', { class: 'ev-think' }, h('summary', {}, L.firstLine(text, 110)), h('div', { class: 'pre-wrap' }, text));
        break;
      case 'tool_call': {
        const m = /^([\w.-]+):\s([\s\S]*)$/.exec(text);
        const tool = e.tool || (m ? m[1] : 'tool');
        const arg = m && (!e.tool || m[1] === e.tool) ? m[2] : text;
        const head = [h('span', { class: 'tool-name mono' }, tool), h('span', { class: 'tool-arg mono' }, L.firstLine(arg, 160))];
        body = arg.includes('\n') || arg.length > 160
          ? h('details', { class: 'tool' }, h('summary', {}, head), h('pre', { class: 'mono' }, arg))
          : h('div', { class: 'tool' }, head);
        break;
      }
      case 'tool_result':
        body = short ? h('div', { class: 'result-line mono' }, text || '(empty)')
          : h('details', { class: 'result' },
            h('summary', { class: 'mono' }, h('span', {}, L.firstLine(text, 110)), h('span', { class: 'dim' }, ` ${lines.length} lines`)),
            h('pre', { class: 'mono' }, text));
        break;
      case 'user':
        body = h('details', { class: 'prompt' }, h('summary', {}, 'Prompt sent to the agent'), h('div', { class: 'pre-wrap' }, text));
        break;
      case 'git':
        body = h('div', { class: 'git mono' }, h('span', { class: 'git-tag' }, 'git'), text);
        break;
      case 'error':
        body = h('div', { class: 'ev-error pre-wrap' }, text);
        break;
      default:
        body = h('div', { class: 'sys mono pre-wrap' }, text);
    }
    return h('div', { class: `ev ev-${kind}`, data: { seq: e.seq } }, time, h('div', { class: 'ev-body' }, body));
  }

  // ---------------------------------------------------------------- chat + result tabs

  function chatFor(t) {
    return app.store.messages.filter((m) => L.messageTouchesTask(m, t));
  }

  function renderTaskChat(panel, t) {
    const list = panel.querySelector('.msgs');
    const msgs = chatFor(t);
    const sig = msgs.map((m) => m.id).join(',');
    if (list.dataset.sig !== sig) {
      list.dataset.sig = sig;
      clear(list);
      if (!msgs.length) list.appendChild(h('li', { class: 'msg-empty' }, `No messages to or from ${t.key} yet.`));
      msgs.forEach((m) => list.appendChild(messageItem(m, true)));
      list.scrollTop = list.scrollHeight;
    }
    renderSayForm(panel.querySelector('form'), t.id);
  }

  function renderResult(panel, t) {
    const sig = [t.result, t.error, t.pr_url, t.branch, t.status].join('\u0001');
    if (panel.dataset.sig === sig) return;
    panel.dataset.sig = sig;
    clear(panel);
    if (!t.result && !t.error && !t.pr_url) {
      panel.appendChild(h('p', { class: 'd-msg' }, L.TERMINAL_STATUSES.has(t.status) ? 'No result was reported.' : 'The agent writes its summary here when it finishes.'));
      return;
    }
    append(panel, [
      t.result ? setMarkdown(h('div', { class: 'md' }), t.result) : null,
      t.error ? h('div', { class: 'ev-error pre-wrap' }, t.error) : null,
      t.pr_url ? h('p', {}, h('span', { class: 'dim' }, 'Pull request '),
        h('a', { class: 'mono', href: t.pr_url, target: '_blank', rel: 'noopener noreferrer' }, t.pr_url)) : null,
      t.branch ? h('p', {}, h('span', { class: 'dim' }, 'Branch '), h('span', { class: 'mono' }, t.branch)) : null,
    ]);
  }

  async function taskAction(t, action) {
    const verbs = { retry: 'Retry requested', cancel: 'Cancelled', done: 'Marked done' };
    try {
      const updated = await api('POST', `/api/tasks/${encodeURIComponent(t.id)}/${action}`);
      if (updated && updated.id) receive({ type: 'task', task: updated });
      toast(`${verbs[action]}: ${t.key}.`);
    } catch (ex) {
      toast(`${t.key}: ${ex.message}`, 'error');
    }
  }

  function copy(text) {
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast('Branch copied.'), () => {});
  }
  const fmtNum = (n) => (n || 0) >= 1000 ? `${((n || 0) / 1000).toFixed((n || 0) >= 100000 ? 0 : 1)}k` : String(n || 0);

  // ================================================================== timers, keyboard, boot

  // tick elapsed counters and relative times without re-rendering
  setInterval(() => {
    const t = now();
    document.querySelectorAll('.elapsed[data-start]').forEach((el) => {
      const start = Number(el.dataset.start);
      if (!start) return;
      const end = el.dataset.end ? Number(el.dataset.end) : t;
      el.textContent = L.fmtDuration(end - start);
    });
    document.querySelectorAll('time.rel[data-ts], .menu-time[data-ts]').forEach((el) => {
      el.textContent = L.relTime(Number(el.dataset.ts), t);
    });
  }, 1000);

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || e.defaultPrevented) return;
    if (document.querySelector('dialog[open]')) return; // the dialog closes itself
    if (!$('project-menu').hidden) { closeMenu(); return; }
    if (app.drawer) { closeDrawer(); return; }
    if (app.railOpen) $('rail-toggle').click();
  });

  window.matchMedia('(max-width: 1180px)').addEventListener('change', () => { app.railUnread = 0; updateRailToggle(); });

  function boot() {
    renderEmpty();
    invalidate('fleet', 'board', 'rail', 'switcher');
    if (params.get('demo') === '1') { startDemo('Demo requested with ?demo=1'); return; }
    if (location.protocol === 'file:') { startDemo('Opened from a file, so there is no hub'); return; }
    setMode('connecting', 'Connecting');
    connectLive();
    resyncOverHttp(); // a quick first paint even before the socket's snapshot
    setTimeout(() => { if (!app.hubSeen) startDemo(`No hub answered within ${DEMO_AFTER_MS / 1000} s`); }, DEMO_AFTER_MS);
  }

  // expose a tiny handle for debugging in the console
  window.OVBoard = { app, receive, openTask, closeDrawer };
  boot();
})();
