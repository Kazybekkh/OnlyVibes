/*
 * OnlyVibes board: in-page demo hub.
 *
 * When the board is opened from file:// or no hub answers within 2.5 s, board.js swaps its
 * network transport for this simulation. It speaks exactly the PROTOCOL.md §4 shapes:
 * it emits WS /board frames (snapshot, project, task, event, message, worker) and answers the
 * same HTTP calls (GET /api/state, GET /api/tasks/{id}, POST /api/projects, /api/messages,
 * task retry/cancel/done, add task), so the UI code path is identical to live mode.
 *
 * Exposed as window.OVDemo (classic script) or a CommonJS module under node:
 *
 *   node -e "const D=require('./web/demo.js'); const h=D.createDemoHub({speed:20});
 *            h.start(f=>console.log(f.type)); setTimeout(()=>h.stop(),5000)"
 *
 * Options: speed (time multiplier, default 1), seed (PRNG seed), withHistory (default true:
 * add an older finished project so the project switcher has something to switch to).
 * No env vars; no network.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.OVDemo = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const DEFAULT_PROMPT = 'Build a pomodoro web app with session history and a weekly stats chart. ' +
    'Keyboard driven, works offline, no account needed.';

  const POMODORO_PLAN = [
    { ref: 'T1', title: 'Scaffold Vite + TypeScript app, lint and CI', priority: 1, depends_on: [], agent_hint: '',
      description: 'Create the **foundation** every other task builds on:\n- Vite + TypeScript, `src/` layout\n- ESLint + Prettier, `npm test` with Vitest\n- GitHub Actions workflow running lint + tests\n\nExport an empty `src/app.ts` mount point that later tasks extend.',
      acceptance: ['npm run build succeeds', 'CI workflow runs lint + tests', 'README explains npm scripts'] },
    { ref: 'T2', title: 'Timer state machine + persistence', priority: 1, depends_on: ['T1'], agent_hint: 'claude-code',
      description: 'Implement `useTimer()` in `src/timer.ts`: states `idle | focus | short_break | long_break | paused`.\nPersist the current state in `localStorage` so a reload resumes the session.\n\nInterface for T4/T6: `timer.subscribe(fn)`, `timer.start()`, `timer.pause()`, `timer.configure(durations)`.',
      acceptance: ['timer survives reload', 'state transitions covered by unit tests'] },
    { ref: 'T3', title: 'Session history store (IndexedDB)', priority: 2, depends_on: ['T1'], agent_hint: '',
      description: 'Record every finished focus session in IndexedDB via a small wrapper in `src/history.ts`.\nExpose `history.add(session)` and `history.byWeek(date)` for the stats chart (T5).',
      acceptance: ['sessions persist across reloads', 'byWeek returns 7 buckets'] },
    { ref: 'T4', title: 'Timer UI + keyboard shortcuts', priority: 2, depends_on: ['T2'], agent_hint: '',
      description: 'Main screen: large countdown, phase label, start/pause/skip.\nShortcuts: `space` start/pause, `s` skip, `r` reset. Uses `useTimer()` from T2.',
      acceptance: ['all controls reachable by keyboard', 'countdown updates every second'] },
    { ref: 'T5', title: 'Weekly stats chart', priority: 3, depends_on: ['T3'], agent_hint: 'codex',
      description: 'A bar chart of focus minutes per day for the current week, from `history.byWeek()`.\nNo chart library; plain SVG.',
      acceptance: ['renders 7 bars', 'empty week shows a hint'] },
    { ref: 'T6', title: 'Settings panel: durations and sounds', priority: 3, depends_on: ['T2'], agent_hint: '',
      description: 'Settings for focus/break lengths and the end-of-phase sound. Calls `timer.configure()`.',
      acceptance: ['settings persist', 'invalid durations rejected'] },
    { ref: 'T7', title: 'End-to-end tests + release README', priority: 4, depends_on: ['T4', 'T5', 'T6'], agent_hint: '',
      description: 'Playwright tests for a full focus → break cycle and the stats view. Update README with screenshots and usage.',
      acceptance: ['e2e suite green in CI', 'README has usage section'] },
  ];

  function genericPlan(title) {
    const t = title.toLowerCase();
    return [
      { ref: 'T1', title: 'Scaffold project, lint and CI', priority: 1, depends_on: [],
        description: `Set up the repository for **${title}**: project layout, lint, test runner, CI workflow.`,
        acceptance: ['build succeeds', 'CI runs tests'] },
      { ref: 'T2', title: 'Core data model + persistence', priority: 1, depends_on: ['T1'],
        description: `Define the data model for ${t} and a storage layer with a small typed API.`,
        acceptance: ['model covered by unit tests'] },
      { ref: 'T3', title: 'Main feature logic', priority: 2, depends_on: ['T2'],
        description: `Implement the core behaviour of ${t} on top of the data model.`,
        acceptance: ['happy path tested', 'errors surfaced to callers'] },
      { ref: 'T4', title: 'Primary UI', priority: 2, depends_on: ['T1'],
        description: 'Build the main screen with placeholder data, then wire it to the feature logic.',
        acceptance: ['keyboard accessible', 'works at 1280px and on mobile'] },
      { ref: 'T5', title: 'Settings and secondary views', priority: 3, depends_on: ['T3'],
        description: 'Settings screen and any secondary views the product needs.',
        acceptance: ['settings persist'] },
      { ref: 'T6', title: 'Tests, docs and release checklist', priority: 4, depends_on: ['T3', 'T4', 'T5'],
        description: 'End-to-end tests and a README with setup and usage.',
        acceptance: ['e2e suite green', 'README complete'] },
    ].map((it) => ({ agent_hint: '', ...it }));
  }

  function titleFromPrompt(prompt) {
    let words = prompt.replace(/^\s*(please\s+)?(build|make|create|write)\s+(me\s+)?(an?|the)?\s*/i, '')
      .split(/\s+/).filter(Boolean).slice(0, 4);
    while (words.length > 1 && /^(with|and|for|a|an|the|of|to|that|in|on)$/i.test(words[words.length - 1])) words.pop();
    words = words.join(' ').replace(/[.,;:]+$/, '');
    return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'Untitled product';
  }

  function slugify(s, max = 40) {
    return s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, max).replace(/-+$/, '');
  }

  // small deterministic PRNG so demo runs (and tests) are reproducible
  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  class HttpError extends Error {
    constructor(status, code, message) { super(message); this.status = status; this.code = code; }
  }

  function createDemoHub(opts = {}) {
    const speed = opts.speed || 1;
    const rand = mulberry32(opts.seed || 7);
    const pick = (arr) => arr[Math.floor(rand() * arr.length)];
    const now = () => Date.now() / 1000;
    const clone = (o) => JSON.parse(JSON.stringify(o));

    const projects = new Map();
    const tasks = new Map();
    const workers = new Map();
    const messages = [];
    const events = new Map(); // task id -> [event]
    const runs = new Map(); // task id -> run token (cancels stale timers)
    const timers = new Set();
    let emit = () => {};
    let msgSeq = 40;
    let prSeq = 11;
    let running = false;

    function later(ms, fn) {
      if (!running) return;
      const id = setTimeout(() => { timers.delete(id); if (running) fn(); }, ms / speed);
      timers.add(id);
    }

    // ---------------------------------------------------------------- emitters
    const sendTask = (t) => { t.updated_at = now(); emit({ type: 'task', task: clone(t) }); };
    const sendProject = (p) => emit({ type: 'project', project: clone(p) });
    const sendWorker = (w) => emit({ type: 'worker', worker: clone(publicWorker(w)) });

    function publicWorker(w) {
      const { online, ...rest } = w;
      rest.status = !online ? 'offline' : w.running.length ? 'busy' : 'idle';
      return rest;
    }

    function addEvent(taskId, kind, text, extra = {}) {
      const list = events.get(taskId) || [];
      events.set(taskId, list);
      const ev = { seq: list.length + 1, ts: now(), kind, text, tool: extra.tool || '', data: extra.data || {} };
      list.push(ev);
      if (list.length > 3000) list.shift();
      const t = tasks.get(taskId);
      if (t) {
        t.event_count = ev.seq;
        if (kind !== 'user') t.last_event = String(text).split('\n')[0].slice(0, 160);
      }
      emit({ type: 'event', task_id: taskId, event: clone(ev) });
      return ev;
    }

    function addMessage(projectId, from, to, text, replyTo = null) {
      const m = { id: `m-${String(++msgSeq).padStart(6, '0')}`, project_id: projectId, ts: now(),
        from, to: to || 'all', text, reply_to: replyTo };
      messages.push(m);
      emit({ type: 'message', message: clone(m) });
      return m;
    }

    const agentFrom = (t) => ({
      kind: 'agent', task_id: t.id, worker_id: t.assignee.worker_id, agent: t.assignee.agent,
      name: `${t.key} · ${t.assignee.agent}@${t.assignee.laptop}`,
    });
    const hubFrom = { kind: 'system', name: 'hub' };

    // ---------------------------------------------------------------- entities
    function makeTask(project, fields) {
      const t = {
        id: '', key: '', project_id: project.id, kind: 'build', title: '', description: '', acceptance: [],
        depends_on: [], priority: 2, agent_hint: '', status: 'todo', assignee: null, progress: 0,
        progress_note: '', last_event: '', branch: '', pr_url: '', result: '', error: '', attempts: 0,
        created_at: now(), started_at: null, finished_at: null, updated_at: now(),
        tokens: { input: 0, output: 0 }, event_count: 0, ...fields,
      };
      tasks.set(t.id, t);
      return t;
    }

    function newProject(prompt, repo, base, createdAt) {
      let id;
      do id = 'p-' + Math.floor(rand() * 0xffff).toString(16).padStart(4, '0'); while (projects.has(id));
      const p = { id, prompt, title: prompt.split(/\s+/).slice(0, 6).join(' '), repo: repo || '',
        base_branch: base || 'main', status: 'planning', created_at: createdAt || now(),
        plan_task_id: `${id}-PLAN`, summary: '' };
      projects.set(id, p);
      makeTask(p, { id: p.plan_task_id, key: 'PLAN', kind: 'plan', priority: 0,
        title: 'Plan the build', description: `Decompose the product into parallel build tasks.\n\n> ${prompt}`,
        acceptance: ['4–12 tasks with explicit interfaces', 'foundation task first'] });
      return p;
    }

    function refreshProject(p) {
      const plan = tasks.get(p.plan_task_id);
      const build = [...tasks.values()].filter((t) => t.project_id === p.id && t.kind === 'build');
      let status = p.status;
      if (plan && plan.status === 'failed') status = 'failed';
      else if (plan && plan.status !== 'done') status = 'planning';
      else if (build.length && build.every((t) => t.status === 'done' || t.status === 'cancelled')
        && build.some((t) => t.status === 'done')) status = 'done';
      else if (build.length) status = 'active';
      if (status !== p.status) { p.status = status; sendProject(p); }
    }

    function reevaluateBacklog(projectId) {
      for (const t of tasks.values()) {
        if (t.project_id !== projectId || t.status !== 'backlog') continue;
        const ready = t.depends_on.every((d) => {
          const dt = tasks.get(d);
          return !dt || dt.status === 'done' || dt.status === 'cancelled';
        });
        if (ready) { t.status = 'todo'; sendTask(t); }
      }
    }

    // ---------------------------------------------------------------- workers + scheduler
    function addWorker(id, owner, ip, agents, max, online) {
      workers.set(id, { id, laptop: id, owner, tailscale_ip: ip, agents, max_concurrent: max, running: [],
        status: 'idle', connected_at: now(), last_seen: now(), version: '2', online });
    }

    function schedule() {
      for (const w of workers.values()) {
        if (!w.online || w.running.length >= w.max_concurrent) continue;
        const candidates = [...tasks.values()].filter((t) => t.status === 'todo'
          && projects.get(t.project_id).status !== 'failed')
          .sort((a, b) => (a.kind === 'plan' ? 0 : 1) - (b.kind === 'plan' ? 0 : 1)
            || a.priority - b.priority || a.created_at - b.created_at
            || Number(a.key.slice(1)) - Number(b.key.slice(1)));
        const t = candidates[0];
        if (!t) return;
        claim(t, w);
      }
    }

    function claim(t, w) {
      const agent = t.agent_hint && w.agents.includes(t.agent_hint) ? t.agent_hint : w.agents[0];
      t.status = 'claimed';
      t.assignee = { worker_id: w.id, laptop: w.laptop, owner: w.owner, agent };
      t.attempts += 1;
      t.error = '';
      t.progress = 0;
      t.progress_note = '';
      w.running.push(t.id);
      const token = {};
      runs.set(t.id, token);
      sendTask(t);
      sendWorker(w);
      later(900 + rand() * 900, () => start(t, w, token));
    }

    function release(t) {
      const w = t.assignee && workers.get(t.assignee.worker_id);
      runs.delete(t.id);
      if (w) {
        w.running = w.running.filter((id) => id !== t.id);
        sendWorker(w);
      }
    }

    function start(t, w, token) {
      if (runs.get(t.id) !== token) return;
      const p = projects.get(t.project_id);
      t.status = 'in_progress';
      t.started_at = now();
      t.finished_at = null;
      if (t.kind === 'build') t.branch = p.repo ? `ov/${t.id.toLowerCase()}-${slugify(t.title)}` : '';
      addEvent(t.id, 'system', `worker ${w.id} started ${t.assignee.agent} (attempt ${t.attempts}) in ` +
        `~/.onlyvibes/workspaces/${p.id}/${p.repo ? t.key : 'shared'}`);
      sendTask(t);
      const steps = t.kind === 'plan' ? planScript(t, p) : buildScript(t, p);
      runSteps(t, token, steps, 0);
    }

    function runSteps(t, token, steps, i) {
      if (runs.get(t.id) !== token) return;
      if (i >= steps.length) return;
      const step = steps[i];
      later(step.wait != null ? step.wait : 700 + rand() * 1600, () => {
        if (runs.get(t.id) !== token) return;
        step.run();
        if (step.tokens !== false) {
          t.tokens.input += Math.floor(800 + rand() * 4000);
          t.tokens.output += Math.floor(80 + rand() * 600);
        }
        sendTask(t);
        runSteps(t, token, steps, i + 1);
      });
    }

    const ev = (t, kind, text, extra) => ({ run: () => addEvent(t.id, kind, text, extra) });
    const progress = (t, pct, note) => ({ run: () => {
      t.progress = pct; t.progress_note = note;
      addEvent(t.id, 'tool_call', `Bash: ov progress ${pct} "${note}"`, { tool: 'Bash' });
    } });

    // ---------------------------------------------------------------- scripted runs
    function planScript(t, p) {
      const items = p.prompt === DEFAULT_PROMPT ? POMODORO_PLAN : genericPlan(titleFromPrompt(p.prompt));
      const title = p.prompt === DEFAULT_PROMPT ? 'Pomodoro app' : titleFromPrompt(p.prompt);
      const json = JSON.stringify({ title, tasks: items.map((x) => ({ ref: x.ref, title: x.title, depends_on: x.depends_on })) }, null, 2);
      return [
        ev(t, 'user', `You are the PLANNER for project ${p.id}.\n\nProduct prompt:\n${p.prompt}\n\nRepo: ${p.repo || '(local mode)'} · base ${p.base_branch}\n\nDecompose into 4–12 build tasks that agents on different laptops can run in parallel. Submit with \`ov plan plan.json\`.`),
        ev(t, 'thinking', 'The repo is probably empty, so a scaffold task has to come first and everything else depends on it. Timer logic and storage can run in parallel after that.'),
        ev(t, 'tool_call', p.repo ? `Bash: gh repo view ${p.repo} --json defaultBranchRef` : 'Bash: ls -la', { tool: 'Bash' }),
        ev(t, 'tool_result', p.repo ? '{"defaultBranchRef":null}\n(empty repository)' : 'total 0'),
        progress(t, 25, 'inspected repo: empty, will bootstrap'),
        ev(t, 'assistant', `The repository is empty. I'll split **${title}** into ${items.length} tasks:\n${items.map((x) => `- ${x.ref}: ${x.title}`).join('\n')}\n\nT1 is the foundation; the rest fan out from it.`),
        ev(t, 'tool_call', 'Write: plan.json', { tool: 'Write' }),
        ev(t, 'tool_result', `Wrote ${json.length} bytes to plan.json`),
        progress(t, 70, 'plan drafted, bootstrapping repo'),
        ...(p.repo ? [
          ev(t, 'git', `git init && git commit -m "Initial commit: product prompt + plan"`),
          ev(t, 'git', `git push -u origin ${p.base_branch}  →  created ${p.repo}@${p.base_branch}`),
        ] : []),
        ev(t, 'tool_call', 'Bash: ov plan plan.json', { tool: 'Bash' }),
        ev(t, 'tool_result', `plan accepted: ${items.length} tasks`),
        ev(t, 'assistant', 'Plan submitted.\n```json\n' + json + '\n```'),
        { run: () => finishPlan(t, p, items, title) },
      ];
    }

    function finishPlan(t, p, items, title) {
      const idByRef = {};
      items.forEach((it, i) => { idByRef[it.ref] = `${p.id}-T${i + 1}`; });
      t.status = 'done';
      t.progress = 100;
      t.progress_note = `planned ${items.length} tasks`;
      t.finished_at = now();
      t.result = `Planned ${items.length} build tasks. T1 is the foundation; the rest fan out from it.`;
      release(t);
      sendTask(t);
      p.title = title;
      p.summary = items === POMODORO_PLAN
        ? 'Foundation first (T1), then timer, storage and UI work in parallel, with end-to-end tests last. ' +
          'Interfaces are named in each task so agents on different laptops can build against them.'
        : `Scaffold first (T1), then the data model, feature logic and UI fan out; tests and docs close it out.`;
      p.status = 'active';
      sendProject(p);
      items.forEach((it, i) => {
        const deps = it.depends_on.map((r) => idByRef[r]).filter(Boolean);
        const nt = makeTask(p, { id: `${p.id}-T${i + 1}`, key: `T${i + 1}`, title: it.title, description: it.description,
          acceptance: it.acceptance, depends_on: deps, priority: it.priority, agent_hint: it.agent_hint || '',
          status: deps.length ? 'backlog' : 'todo', created_at: now() + i * 0.001 });
        sendTask(nt);
      });
      addMessage(p.id, hubFrom, 'all', `Plan ready: ${items.length} tasks. T1 starts first; the rest unblock as dependencies merge.`);
    }

    function buildScript(t, p) {
      const slug = slugify(t.title, 18) || 'feature';
      const file = `src/${slug.split('-')[0]}.ts`;
      const deps = t.depends_on.map((d) => tasks.get(d)).filter(Boolean);
      const dependents = [...tasks.values()].filter((x) => x.depends_on.includes(t.id));
      const failThisRun = t.key === 'T4' && t.attempts === 1;
      const leaveOpen = !!p.repo && dependents.length === 0 && t.key !== 'T1' && t.attempts === 1 && t.kind === 'build'
        && [...tasks.values()].filter((x) => x.project_id === p.id && x.kind === 'build').length > 3;
      const steps = [
        ev(t, 'user', `You are ${t.key} on project "${p.title}".\n\nTask: ${t.title}\n\n${t.description}\n\nAcceptance:\n${t.acceptance.map((a) => '- ' + a).join('\n')}\n\nUse \`ov say\`, \`ov inbox\`, \`ov progress\`. Commit your work; the worker pushes.`),
        ev(t, 'thinking', deps.length
          ? `${deps.map((d) => d.key).join(' and ')} already merged. I should read what they export before writing anything.`
          : 'Fresh repo. Start with the layout everyone else will build on and keep the scripts obvious.'),
        ev(t, 'tool_call', 'Bash: git log --oneline -5', { tool: 'Bash' }),
        ev(t, 'tool_result', deps.length
          ? deps.map((d, i) => `${(0xa1b2c3 + i * 4099).toString(16)} ${d.key}: ${d.title} (#${12 + i})`).join('\n')
          : 'e3f1a20 Initial commit: product prompt + plan'),
        ev(t, 'tool_call', `Read: ${deps.length ? 'src/app.ts' : 'README.md'}`, { tool: 'Read' }),
        ev(t, 'tool_result', deps.length
          ? "import { mount } from './mount';\n\nexport function app(root: HTMLElement) {\n  mount(root);\n}"
          : `# ${p.title}\n\n${p.prompt}`),
        progress(t, 15, 'read the codebase, planning changes'),
        ev(t, 'assistant', `I'll implement **${t.title.toLowerCase()}** in \`${file}\` and keep the public surface small:\n- one exported entry point\n- unit tests next to the module\n\nChecking the inbox for interface notes first.`),
        ev(t, 'tool_call', 'Bash: ov inbox', { tool: 'Bash' }),
        ev(t, 'tool_result', deps.length ? `[${deps[0].key}] exported from ${file.replace(slug.split('-')[0], 'timer')}; import from there` : '(no new messages)'),
        { run: () => {
          const d = dependents.find((x) => !['done', 'cancelled'].includes(x.status));
          addMessage(p.id, agentFrom(t), d ? d.id : 'all', d ? pick([
            `${d.key}: my exports will live in ${file}. Import from there, not from internals.`,
            `${d.key}: heads up, I'm keeping the public surface of ${file} to one entry point. I'll post the signature when it's final.`,
            `${d.key}: you'll need ${file} from me. ETA a few minutes; tests first.`,
          ]) : pick([
            `Starting ${t.key} on ${t.branch || 'the shared workspace'}. Shout if you touch ${file}.`,
            `${t.key} here: I'm only touching ${file} and its test. Nothing shared.`,
          ]));
        } },
        ev(t, 'tool_call', `Edit: ${file}`, { tool: 'Edit' }),
        ev(t, 'tool_result', `Applied 1 edit to ${file} (+${40 + Math.floor(rand() * 80)} −${Math.floor(rand() * 9)})`),
        progress(t, 45, `${file} written, adding tests`),
        ev(t, 'tool_call', `Write: ${file.replace('.ts', '.test.ts')}`, { tool: 'Write' }),
        ev(t, 'tool_result', `Wrote ${file.replace('.ts', '.test.ts')}`),
        ev(t, 'tool_call', 'Bash: npm test -- --run', { tool: 'Bash' }),
      ];
      if (failThisRun) {
        steps.push(
          ev(t, 'tool_result', ' FAIL  src/timer.test.ts > resumes after reload\n   expected "focus" to be "paused"\n Tests  1 failed | 13 passed'),
          progress(t, 60, 'one test failing, investigating'),
          ev(t, 'thinking', 'The persisted state is read before the store hydrates. That is a T2 interface question, not mine.'),
          ev(t, 'error', 'Agent exited with code 1: test suite failing after 3 fix attempts (resumes after reload).'),
          { run: () => failTask(t, 'agent exit 1: test suite failing (resumes after reload)') },
        );
        return steps;
      }
      steps.push(
        ev(t, 'tool_result', ` ✓ ${file.replace('.ts', '.test.ts')} (${6 + Math.floor(rand() * 9)} tests)\n Tests  all passed`),
        progress(t, 80, 'tests green, committing'),
        ev(t, 'tool_call', `Bash: git add -A && git commit -m "${t.key}: ${t.title}"`, { tool: 'Bash' }),
        ev(t, 'tool_result', `[${t.branch || 'main'} ${(0x5f00aa + t.attempts * 77).toString(16)}] ${t.key}: ${t.title}\n 2 files changed`),
        ev(t, 'assistant', `Done. ${t.title} is in \`${file}\` with tests. ${dependents.length ? `${dependents.map((d) => d.key).join(', ')} can build on it now.` : 'Nothing else depends on this.'}`),
        { run: () => toReview(t, p) },
      );
      if (p.repo) {
        steps.push(
          ev(t, 'git', `git fetch && git rebase origin/${p.base_branch}  →  clean`, { tool: '' }),
          ev(t, 'git', `git push -u origin ${t.branch}`),
          { run: () => { t.pr_url = `https://github.com/${p.repo}/pull/${++prSeq}`; addEvent(t.id, 'git', `gh pr create  →  ${t.pr_url}`); } },
        );
        if (!leaveOpen) steps.push(ev(t, 'git', 'gh pr merge --squash --delete-branch  →  merged'));
        else steps.push(ev(t, 'system', 'PR left open for human review (touches CI config); AUTO_MERGE skipped'));
      } else {
        steps.push(ev(t, 'git', 'local mode: committed in the shared workspace, no push'));
      }
      steps.push({ tokens: false, run: () => finishOk(t, p, !p.repo || !leaveOpen) });
      return steps;
    }

    function toReview(t) {
      t.status = 'review';
      t.progress = 95;
      t.progress_note = 'pushing branch, opening PR';
    }

    function finishOk(t, p, merged) {
      t.progress = 100;
      t.result = `${t.title}: implemented with unit tests.${t.pr_url ? ` PR ${t.pr_url}${merged ? ' merged' : ' open for review'}.` : ''}`;
      if (merged) {
        t.status = 'done';
        t.finished_at = now();
        t.progress_note = p.repo ? 'merged' : 'done (local mode)';
      } else {
        t.status = 'review';
        t.progress_note = 'PR open, waiting for a human';
      }
      release(t);
      sendTask(t);
      if (merged) reevaluateBacklog(p.id);
      refreshProject(p);
    }

    function failTask(t, error) {
      const p = projects.get(t.project_id);
      t.error = error;
      t.status = t.attempts < 3 ? 'todo' : 'failed';
      if (t.status === 'failed') t.finished_at = now();
      release(t);
      t.assignee = t.status === 'failed' ? t.assignee : null;
      addEvent(t.id, 'system', t.status === 'todo' ? `attempt ${t.attempts} failed; back to todo for retry` : 'giving up after 3 attempts');
      sendTask(t);
      if (t.status === 'todo') {
        addMessage(p.id, hubFrom, 'all', `${t.key} failed on attempt ${t.attempts}: ${error}. Requeued.`);
      }
      refreshProject(p);
    }

    // ---------------------------------------------------------------- human replies
    function replyToHuman(m) {
      const p = projects.get(m.project_id);
      if (!p) return;
      const target = tasks.get(m.to);
      const live = [...tasks.values()].filter((t) => t.project_id === p.id && t.assignee
        && (t.status === 'in_progress' || t.status === 'claimed'));
      const targetRunning = target && target.assignee && (target.status === 'in_progress' || target.status === 'claimed');
      const responder = targetRunning ? target : (target ? null : pick(live.length ? live : [null]));
      later(1800 + rand() * 2200, () => {
        if (!responder) {
          addMessage(p.id, hubFrom, 'all', target
            ? `${target.key} is not running right now. Its agent reads this with \`ov inbox\` if the task runs again.`
            : 'No agent is running right now; the message stays in the inbox for the next one.', m.id);
          return;
        }
        addMessage(p.id, agentFrom(responder), 'all', pick([
          `Got it. Folding that into ${responder.key} before I commit.`,
          `Ack. I'll pick it up on my next \`ov inbox\` check and adjust ${responder.key}.`,
          `Noted. That changes the interface slightly; I'll post the new signature here.`,
        ]), m.id);
      });
    }

    // ---------------------------------------------------------------- seed data
    function seed() {
      addWorker('endermite', 'carlo', '100.124.163.102', ['claude-code', 'codex'], 2, true);
      addWorker('kay-mbp', 'kay', '100.64.0.11', ['vibe', 'cursor'], 1, true);
      addWorker('amina-air', 'amina', '100.64.0.27', ['codex', 'claude-code'], 1, true);
      addWorker('dao-legion', 'dao', '100.64.0.42', ['cursor', 'mock'], 1, false);

      if (opts.withHistory !== false) {
        const old = newProject('Build a terminal weather dashboard with a 5 day forecast', 'acme/weather-tui', 'main', now() - 86400);
        old.title = 'Weather TUI';
        old.status = 'done';
        old.summary = 'Three parallel slices on top of a scaffold: API client, layout, caching.';
        const plan = tasks.get(old.plan_task_id);
        Object.assign(plan, { status: 'done', progress: 100, attempts: 1, started_at: old.created_at + 5,
          finished_at: old.created_at + 70, result: 'Planned 4 build tasks.' });
        ['Scaffold Python CLI + CI', 'Forecast API client', 'Terminal layout', 'Response caching'].forEach((title, i) => {
          const st = old.created_at + 100 + i * 60;
          makeTask(old, { id: `${old.id}-T${i + 1}`, key: `T${i + 1}`, title, status: 'done', progress: 100,
            depends_on: i ? [`${old.id}-T1`] : [], attempts: 1, started_at: st, finished_at: st + 180 + i * 40,
            created_at: old.created_at + 80, branch: `ov/${old.id}-t${i + 1}-${slugify(title)}`,
            pr_url: `https://github.com/acme/weather-tui/pull/${i + 2}`, result: `${title} merged.`,
            assignee: { worker_id: 'endermite', laptop: 'endermite', owner: 'carlo', agent: 'claude-code' },
            tokens: { input: 41000 + i * 5000, output: 6200 + i * 800 } });
        });
        messages.push({ id: 'm-000003', project_id: old.id, ts: old.created_at + 400, to: 'all', reply_to: null,
          from: { kind: 'system', name: 'hub' }, text: 'All 4 tasks merged. Project done.' });
      }

      const p = newProject(DEFAULT_PROMPT, 'acme/pomodoro', 'main', now() - 2);
      messages.push({ id: 'm-000010', project_id: p.id, ts: now() - 1, to: 'all', reply_to: null,
        from: { kind: 'human', name: 'carlo' }, text: 'Keep the bundle small, no UI framework please.' });
    }

    // ---------------------------------------------------------------- HTTP surface
    function snapshot() {
      return {
        projects: [...projects.values()].map(clone),
        tasks: [...tasks.values()].map(clone),
        workers: [...workers.values()].map((w) => clone(publicWorker(w))),
        messages: messages.slice(-300).map(clone),
      };
    }

    function need(cond, status, code, message) { if (!cond) throw new HttpError(status, code, message); }

    function handle(method, path, body) {
      body = body || {};
      let m;
      if (method === 'GET' && path === '/api/state') return snapshot();
      if (method === 'POST' && path === '/api/projects') {
        const prompt = String(body.prompt || '').trim();
        need(prompt, 400, 'bad_request', 'prompt is required');
        const repo = String(body.repo || '').trim();
        need(!repo || /^[\w.-]+\/[\w.-]+$/.test(repo), 400, 'bad_request', 'repo must look like owner/name');
        const p = newProject(prompt, repo, String(body.base_branch || 'main').trim() || 'main');
        sendProject(p);
        sendTask(tasks.get(p.plan_task_id));
        return clone(p);
      }
      if ((m = /^\/api\/tasks\/([^/]+)$/.exec(path)) && method === 'GET') {
        const t = tasks.get(decodeURIComponent(m[1]));
        need(t, 404, 'not_found', 'no such task');
        return { task: clone(t), events: clone(events.get(t.id) || []) };
      }
      if ((m = /^\/api\/tasks\/([^/]+)\/(retry|cancel|done)$/.exec(path)) && method === 'POST') {
        const t = tasks.get(decodeURIComponent(m[1]));
        need(t, 404, 'not_found', 'no such task');
        const p = projects.get(t.project_id);
        if (m[2] === 'retry') {
          need(!['claimed', 'in_progress'].includes(t.status), 409, 'conflict', 'task is running');
          Object.assign(t, { status: 'todo', attempts: 0, error: '', assignee: null, progress: 0, progress_note: '', finished_at: null });
          addEvent(t.id, 'system', 'retry requested by a human');
        } else if (m[2] === 'cancel') {
          need(!['done', 'cancelled'].includes(t.status), 409, 'conflict', `task is already ${t.status}`);
          const wasRunning = runs.has(t.id);
          if (wasRunning) release(t);
          Object.assign(t, { status: 'cancelled', finished_at: now() });
          addEvent(t.id, 'system', wasRunning ? 'cancelled by a human; cancel_task sent to the worker' : 'cancelled by a human');
          reevaluateBacklog(p.id);
        } else {
          need(t.status === 'review', 409, 'conflict', 'only tasks in review can be marked done');
          Object.assign(t, { status: 'done', finished_at: now(), progress: 100, progress_note: 'accepted by a human' });
          addEvent(t.id, 'system', 'marked done by a human');
          reevaluateBacklog(p.id);
        }
        sendTask(t);
        refreshProject(p);
        return clone(t);
      }
      if ((m = /^\/api\/projects\/([^/]+)\/tasks$/.exec(path)) && method === 'POST') {
        const p = projects.get(decodeURIComponent(m[1]));
        need(p, 404, 'not_found', 'no such project');
        const title = String(body.title || '').trim();
        need(title, 400, 'bad_request', 'title is required');
        const n = [...tasks.values()].filter((t) => t.project_id === p.id && t.kind === 'build').length + 1;
        const deps = (body.depends_on || []).filter((d) => tasks.has(d));
        const blocked = deps.some((d) => !['done', 'cancelled'].includes(tasks.get(d).status));
        const t = makeTask(p, { id: `${p.id}-T${n}`, key: `T${n}`, title, description: String(body.description || ''),
          acceptance: body.acceptance || [], depends_on: deps, priority: body.priority || 2,
          agent_hint: body.agent_hint || '', status: blocked ? 'backlog' : 'todo' });
        sendTask(t);
        refreshProject(p);
        return clone(t);
      }
      if (method === 'POST' && path === '/api/messages') {
        const text = String(body.text || '').trim();
        need(text, 400, 'bad_request', 'text is required');
        need(projects.has(body.project_id), 404, 'not_found', 'no such project');
        const msg = addMessage(body.project_id, { kind: 'human', name: body.name || 'human' }, body.to || 'all', text);
        replyToHuman(msg);
        return clone(msg);
      }
      if (method === 'GET' && path === '/api/workers') return [...workers.values()].map((w) => clone(publicWorker(w)));
      throw new HttpError(404, 'not_found', `${method} ${path} is not part of the demo`);
    }

    return {
      DEFAULT_PROMPT,
      start(onFrame) {
        emit = onFrame || (() => {});
        running = true;
        seed();
        emit({ type: 'snapshot', ...snapshot() });
        const tick = () => { schedule(); later(1000, tick); };
        later(1200, tick);
        // a fourth laptop joins a little later, so the fleet strip shows a transition
        later(26000, () => { const w = workers.get('dao-legion'); w.online = true; w.connected_at = now(); sendWorker(w); });
        const heartbeat = () => {
          for (const w of workers.values()) if (w.online) w.last_seen = now();
          emit({ type: 'ping', ts: now() });
          later(15000, heartbeat);
        };
        later(15000, heartbeat);
      },
      stop() {
        running = false;
        timers.forEach(clearTimeout);
        timers.clear();
      },
      /** Promise-based twin of the hub's HTTP API. Rejects with {status, code, message}. */
      request(method, path, body) {
        return new Promise((resolve, reject) => {
          setTimeout(() => {
            try { resolve(handle(method, path.split('?')[0], body)); } catch (e) {
              reject(e instanceof HttpError ? e : new HttpError(500, 'internal', String(e && e.message || e)));
            }
          }, 120);
        });
      },
    };
  }

  return { createDemoHub, DEFAULT_PROMPT, titleFromPrompt, slugify };
});
