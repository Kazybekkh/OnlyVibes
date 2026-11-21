/*
 * OnlyVibes board: pure helpers shared by board.js and demo.js (no DOM access).
 *
 * Loaded as a classic <script> (so the board also works from file://) and exposed as
 * window.OVLib; under node it is a CommonJS module, which is how the helpers are tested:
 *
 *   node --check web/lib.js
 *   node -e "const L = require('./web/lib.js'); console.log(L.renderMarkdown('**hi** <b>'))"
 *
 * Contents
 *   - escapeHtml / renderMarkdown: tiny safe markdown (escape first, then code, bold, lists,
 *     http(s) links). Its output is the ONLY html string the board ever assigns to innerHTML.
 *   - createStore / applyFrame: board state + reducer for WS /board frames (PROTOCOL.md §4).
 *   - column mapping, sorting, dependency hints, project stats, time formatting.
 *
 * No env vars; no network.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.OVLib = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // ------------------------------------------------------------------ escaping + markdown

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC[c]);
  }

  // Inline formatting on ALREADY-ESCAPED text. Code spans and links are swapped for
  // placeholders first so bold/autolink never reach inside them.
  function renderInline(escaped) {
    const slots = [];
    const stash = (html) => `\u0000${slots.push(html) - 1}\u0000`;
    let s = escaped.replace(/\u0000/g, '');
    s = s.replace(/`([^`\n]+)`/g, (_, code) => stash(`<code>${code}</code>`));
    // [label](http(s)://url) — url is escaped text, so it cannot break out of the attribute
    s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, label, url) => stash(`<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`));
    // bare http(s) urls; trailing punctuation stays outside the link
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<\u0000]+)/g, (_, pre, url) => {
      const m = url.match(/^(.*?)([.,;:!?)]*)$/);
      return pre + stash(`<a href="${m[1]}" target="_blank" rel="noopener noreferrer">${m[1]}</a>`) + m[2];
    });
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/__([^_\n]+)__/g, '<strong>$1</strong>');
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[Number(i)]);
  }

  /**
   * Render a small markdown subset to an HTML string that is safe to assign to innerHTML.
   * Supported: ``` fenced code, `code`, **bold**, - / * / 1. lists, # headings (as h4),
   * [label](https://…) and bare http(s) links, paragraphs with single-newline breaks.
   */
  function renderMarkdown(src) {
    const lines = String(src == null ? '' : src).replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let para = [];
    let list = null; // {tag, items}
    const flushPara = () => {
      if (para.length) out.push(`<p>${para.map((l) => renderInline(escapeHtml(l))).join('<br>')}</p>`);
      para = [];
    };
    const flushList = () => {
      if (list) out.push(`<${list.tag}>${list.items.map((i) => `<li>${renderInline(escapeHtml(i))}</li>`).join('')}</${list.tag}>`);
      list = null;
    };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const fence = line.match(/^\s*```/);
      if (fence) {
        flushPara(); flushList();
        const body = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) body.push(lines[i++]);
        out.push(`<pre><code>${escapeHtml(body.join('\n'))}</code></pre>`);
        continue;
      }
      const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (bullet || ordered) {
        flushPara();
        const tag = bullet ? 'ul' : 'ol';
        if (list && list.tag !== tag) flushList();
        if (!list) list = { tag, items: [] };
        list.items.push((bullet || ordered)[1]);
        continue;
      }
      const heading = line.match(/^\s*#{1,6}\s+(.*)$/);
      if (heading) {
        flushPara(); flushList();
        out.push(`<h4>${renderInline(escapeHtml(heading[1]))}</h4>`);
        continue;
      }
      if (!line.trim()) { flushPara(); flushList(); continue; }
      if (list && /^\s{2,}\S/.test(line)) { list.items[list.items.length - 1] += ' ' + line.trim(); continue; }
      flushList();
      para.push(line);
    }
    flushPara(); flushList();
    return out.join('');
  }

  // ------------------------------------------------------------------ board model

  const COLUMNS = [
    { id: 'backlog', label: 'Backlog', statuses: ['backlog'] },
    { id: 'todo', label: 'Todo', statuses: ['todo'] },
    { id: 'progress', label: 'In progress', statuses: ['claimed', 'in_progress'] },
    { id: 'review', label: 'Review', statuses: ['review'] },
    { id: 'done', label: 'Done', statuses: ['done', 'failed', 'cancelled'] },
  ];
  const STATUS_TO_COLUMN = {};
  COLUMNS.forEach((c) => c.statuses.forEach((s) => { STATUS_TO_COLUMN[s] = c.id; }));

  const STATUS_LABEL = {
    backlog: 'Backlog', todo: 'Todo', claimed: 'Claimed', in_progress: 'In progress',
    review: 'Review', done: 'Done', failed: 'Failed', cancelled: 'Cancelled',
  };
  const ACTIVE_STATUSES = new Set(['claimed', 'in_progress', 'review']);
  const TERMINAL_STATUSES = new Set(['done', 'failed', 'cancelled']);

  function columnOf(task) { return STATUS_TO_COLUMN[task.status] || 'backlog'; }

  // Natural order for keys: PLAN first, then T1 < T2 < T10.
  function keyRank(key) {
    if (key === 'PLAN') return -1;
    const m = /^T(\d+)$/.exec(key || '');
    return m ? Number(m[1]) : 1e9;
  }
  function compareKeys(a, b) {
    return keyRank(a.key) - keyRank(b.key) || String(a.key).localeCompare(String(b.key));
  }

  // Stable in-column order (stable order keeps FLIP movement meaningful).
  function sortColumn(colId, tasks) {
    const arr = tasks.slice();
    if (colId === 'done') {
      arr.sort((a, b) => (b.finished_at || b.updated_at || 0) - (a.finished_at || a.updated_at || 0) || compareKeys(a, b));
    } else if (colId === 'todo' || colId === 'backlog') {
      arr.sort((a, b) => (a.kind === 'plan' ? -1 : 0) - (b.kind === 'plan' ? -1 : 0)
        || (a.priority ?? 3) - (b.priority ?? 3) || compareKeys(a, b));
    } else {
      arr.sort(compareKeys);
    }
    return arr;
  }

  /** Keys of dependencies that are not yet satisfied (done or cancelled count as satisfied). */
  function pendingDeps(task, tasksById) {
    return (task.depends_on || []).filter((id) => {
      const d = tasksById.get(id);
      return !d || (d.status !== 'done' && d.status !== 'cancelled');
    }).map((id) => (tasksById.get(id) || {}).key || id.split('-').pop());
  }

  /** {done, total, failed} over BUILD tasks; cancelled tasks are left out of total. */
  function projectStats(tasks) {
    let done = 0, total = 0, failed = 0;
    for (const t of tasks) {
      if (t.kind !== 'build' || t.status === 'cancelled') continue;
      total++;
      if (t.status === 'done') done++;
      if (t.status === 'failed') failed++;
    }
    return { done, total, failed };
  }

  function firstLine(text, max = 160) {
    const line = String(text || '').split('\n').find((l) => l.trim()) || '';
    const t = line.trim();
    return t.length > max ? t.slice(0, max - 1) + '…' : t;
  }

  /** Merge transcript events, dedupe by seq, keep ascending order. Returns a new array. */
  function mergeEvents(existing, incoming) {
    const bySeq = new Map();
    for (const e of existing || []) if (e && e.seq != null) bySeq.set(e.seq, e);
    for (const e of incoming || []) if (e && e.seq != null) bySeq.set(e.seq, e);
    return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  }

  // ------------------------------------------------------------------ agents / workers

  const AGENTS = {
    'claude-code': { letter: 'C', cls: 'ag-claude' },
    codex: { letter: 'X', cls: 'ag-codex' },
    vibe: { letter: 'V', cls: 'ag-vibe' },
    cursor: { letter: 'R', cls: 'ag-cursor' },
    mock: { letter: 'M', cls: 'ag-mock' },
  };
  function agentMeta(name) {
    return AGENTS[name] || { letter: String(name || '?').charAt(0).toUpperCase() || '?', cls: 'ag-other' };
  }

  function prNumber(url) {
    const m = /\/pull\/(\d+)/.exec(url || '');
    return m ? m[1] : '';
  }

  // ------------------------------------------------------------------ time

  function fmtDuration(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return `${m}m ${String(s).padStart(2, '0')}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${String(m % 60).padStart(2, '0')}m`;
  }

  function relTime(ts, now) {
    const d = Math.max(0, (now || Date.now() / 1000) - (ts || 0));
    if (d < 10) return 'now';
    if (d < 60) return `${Math.floor(d)}s`;
    if (d < 3600) return `${Math.floor(d / 60)}m`;
    if (d < 86400) return `${Math.floor(d / 3600)}h`;
    return `${Math.floor(d / 86400)}d`;
  }

  /** Seconds a task has been running (live) or ran (finished); null if it never started. */
  function taskElapsed(task, now) {
    if (!task.started_at) return null;
    const end = task.finished_at || (ACTIVE_STATUSES.has(task.status) ? now : task.updated_at || now);
    return Math.max(0, end - task.started_at);
  }

  function clockTime(ts) {
    const d = new Date((ts || 0) * 1000);
    return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
  }

  // ------------------------------------------------------------------ store + reducer

  function createStore() {
    return { projects: new Map(), tasks: new Map(), workers: new Map(), messages: [], messageIds: new Set() };
  }

  const MAX_MESSAGES = 2000;
  function addMessage(store, m) {
    if (!m || !m.id || store.messageIds.has(m.id)) return false;
    store.messageIds.add(m.id);
    const arr = store.messages;
    // messages almost always arrive in order; insert from the end otherwise
    let i = arr.length;
    while (i > 0 && (arr[i - 1].ts || 0) > (m.ts || 0)) i--;
    arr.splice(i, 0, m);
    if (arr.length > MAX_MESSAGES) store.messageIds.delete(arr.shift().id);
    return true;
  }

  /**
   * Apply one WS /board frame. Returns a change descriptor
   * {type, id?, item?, isNew?} or null for frames that change nothing (ping, unknown).
   */
  function applyFrame(store, f) {
    if (!f || typeof f !== 'object') return null;
    switch (f.type) {
      case 'snapshot': {
        store.projects = new Map((f.projects || []).map((p) => [p.id, p]));
        store.tasks = new Map((f.tasks || []).map((t) => [t.id, t]));
        store.workers = new Map((f.workers || []).map((w) => [w.id, w]));
        store.messages = [];
        store.messageIds = new Set();
        (f.messages || []).forEach((m) => addMessage(store, m));
        return { type: 'snapshot' };
      }
      case 'project': {
        if (!f.project || !f.project.id) return null;
        const isNew = !store.projects.has(f.project.id);
        store.projects.set(f.project.id, f.project);
        return { type: 'project', id: f.project.id, isNew };
      }
      case 'task': {
        if (!f.task || !f.task.id) return null;
        const prev = store.tasks.get(f.task.id);
        store.tasks.set(f.task.id, f.task);
        return { type: 'task', id: f.task.id, isNew: !prev, prevStatus: prev ? prev.status : null };
      }
      case 'event': {
        if (!f.task_id || !f.event) return null;
        const t = store.tasks.get(f.task_id);
        if (t) {
          // keep the card line live even if the task frame lags behind the event frame
          const line = firstLine(f.event.text);
          if (line && f.event.kind !== 'user') t.last_event = line;
          if (f.event.seq != null) t.event_count = Math.max(t.event_count || 0, f.event.seq);
        }
        return { type: 'event', id: f.task_id, item: f.event };
      }
      case 'message':
        return addMessage(store, f.message) ? { type: 'message', id: f.message.id, item: f.message } : null;
      case 'worker': {
        if (!f.worker || !f.worker.id) return null;
        store.workers.set(f.worker.id, f.worker);
        return { type: 'worker', id: f.worker.id };
      }
      default:
        return null; // ping and unknown frames
    }
  }

  function projectTasks(store, projectId) {
    const out = [];
    for (const t of store.tasks.values()) if (t.project_id === projectId) out.push(t);
    return out;
  }

  function projectsByRecency(store) {
    return [...store.projects.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  }

  /** Does a chat message belong to a task's conversation? */
  function messageTouchesTask(m, task) {
    if (!m || !task) return false;
    if (m.to === task.id || (m.from && m.from.task_id === task.id)) return true;
    return false;
  }

  /** Human-readable recipient: "everyone", a task key, or a worker id. */
  function recipientLabel(to, store) {
    if (!to || to === 'all') return 'everyone';
    const t = store.tasks.get(to);
    if (t) return t.key;
    return to;
  }

  return {
    escapeHtml, renderMarkdown, renderInline,
    COLUMNS, STATUS_LABEL, ACTIVE_STATUSES, TERMINAL_STATUSES,
    columnOf, compareKeys, sortColumn, pendingDeps, projectStats, firstLine, mergeEvents,
    agentMeta, prNumber,
    fmtDuration, relTime, taskElapsed, clockTime,
    createStore, applyFrame, addMessage, projectTasks, projectsByRecency, messageTouchesTask, recipientLabel,
  };
});
