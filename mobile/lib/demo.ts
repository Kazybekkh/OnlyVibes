// Standalone demo data — mirrors web/demo.js (orchestra) + the legacy FAKE
// laptops, so every tab works with no server running.
import type { Snapshot } from './types';

const FAKE = [
  { laptop: 'kay-mbp', owner: 'kay', tailscale_ip: '100.64.0.11',
    agents: [{ name: 'vibe · OnlyVibes', pid: 3001, cpu: 42.1, cwd: '/Users/kay/OnlyVibes', last_log: '⏺ Edit server/main.py', summary: 'editing the FastAPI server' },
             { name: 'cursor', pid: 3018, cpu: 12.4, cwd: '/Users/kay/OnlyVibes', last_log: 'Agent: web/index.html', summary: 'tweaking the 3D view' }] },
  { laptop: 'amina-air', owner: 'amina', tailscale_ip: '100.64.0.27',
    agents: [{ name: 'claude-code · agent-app', pid: 4102, cpu: 65.0, cwd: '/Users/amina/agent-app', last_log: 'Read reporter/reporter.py', summary: 'reading the reporter code' },
             { name: 'codex', pid: 4117, cpu: 30.7, cwd: '/Users/amina/agent-app', last_log: 'patched 3 files', summary: 'patching three files' }] },
  { laptop: 'dao-legion', owner: 'dao', tailscale_ip: '100.64.0.42',
    agents: [{ name: 'codex · xr', pid: 5201, cpu: 8.2, cwd: '/Users/dao/xr', last_log: 'reasoning…', summary: 'thinking about WebXR' },
             { name: 'vibe', pid: 5233, cpu: 51.9, cwd: '/Users/dao/xr', last_log: '⏺ Ran pytest — 12 passed', summary: 'running the test suite' },
             { name: 'claude-code · xr › Explore', pid: 5250, cpu: 3.3, cwd: '/Users/dao/xr', last_log: 'TodoWrite 3/5', summary: 'updating the todo list' }] },
];

const DEMO_PROJECTS = [
  { id: 'p-demo1', prompt: 'Build a pomodoro web app with stats', title: 'Pomodoro app',
    repo: 'acme/pomodoro', base_branch: 'main', status: 'active', created_at: 0,
    plan_task_id: 'p-demo1-PLAN', summary: 'Timer state machine, stats view, persistence.' },
];

const DEMO_TASKS = [
  { id: 'p-demo1-PLAN', key: 'PLAN', project_id: 'p-demo1', kind: 'plan', title: 'Plan: Pomodoro app',
    status: 'done', assignee: { worker_id: 'kay-mbp', laptop: 'kay-mbp', owner: 'kay', agent: 'claude-code' },
    progress: 100, progress_note: 'plan approved', last_event: 'plan written', priority: 0, branch: '', pr_url: '' },
  { id: 'p-demo1-T1', key: 'T1', project_id: 'p-demo1', kind: 'build', title: 'Timer state machine + persistence',
    status: 'in_progress', assignee: { worker_id: 'kay-mbp', laptop: 'kay-mbp', owner: 'kay', agent: 'vibe' },
    progress: 62, progress_note: 'IndexedDB layer done', last_event: 'Bash: npm test — 41 passed', priority: 2, branch: 'ov/p-demo1-T1', pr_url: '' },
  { id: 'p-demo1-T2', key: 'T2', project_id: 'p-demo1', kind: 'build', title: 'Stats view with weekly chart',
    status: 'todo', assignee: null, progress: 0, progress_note: '', last_event: '', priority: 3, branch: '', pr_url: '' },
  { id: 'p-demo1-T3', key: 'T3', project_id: 'p-demo1', kind: 'build', title: 'Deploy to Pages + PR',
    status: 'failed', assignee: { worker_id: 'dao-legion', laptop: 'dao-legion', owner: 'dao', agent: 'codex' },
    progress: 30, progress_note: '', last_event: 'push rejected: branch protection', priority: 4, branch: 'ov/p-demo1-T3', pr_url: '', error: 'push rejected: branch protection' },
];

const DEMO_WORKERS = [
  { id: 'kay-mbp', laptop: 'kay-mbp', owner: 'kay', tailscale_ip: '100.64.0.11',
    agents: ['vibe', 'claude-code'], max_concurrent: 2, running: ['p-demo1-T1'], status: 'busy', last_seen: 0 },
  { id: 'dao-legion', laptop: 'dao-legion', owner: 'dao', tailscale_ip: '100.64.0.42',
    agents: ['codex'], max_concurrent: 1, running: [], status: 'idle', last_seen: 0 },
];

const DEMO_MESSAGES = [
  { id: 'm-000002', project_id: 'p-demo1', ts: 0, from: { kind: 'agent', task_id: 'p-demo1-T1', worker_id: 'kay-mbp', agent: 'vibe', name: 'T1 · vibe@kay-mbp' },
    to: 'all', text: "I'm exposing useTimer() from src/timer.ts — T2 please import it from there", reply_to: null },
  { id: 'm-000001', project_id: 'p-demo1', ts: 0, from: { kind: 'human', name: 'kay' },
    to: 'all', text: 'Ship the pomodoro app by Friday', reply_to: null },
];

export function demoSnapshot(tick: number): Snapshot {
  const now = Date.now() / 1000;
  const jitter = (base: number, seed: number) => Math.max(1, base + Math.sin(tick + seed) * 8);
  return {
    type: 'state',
    ts: now,
    laptops: FAKE.map((l) => ({
      ...l,
      ts: now,
      agents: l.agents.map((a, i) => ({
        ...a,
        cpu: jitter(a.cpu, a.pid + i),
        kind: i === 0 ? 'session' : 'app',
        agent_id: `${l.tailscale_ip}:${a.name}:${a.pid}`,
      })),
    })),
    projects: DEMO_PROJECTS.map((p) => ({ ...p, created_at: now - 3600 })),
    tasks: DEMO_TASKS.map((t, i) => ({ ...t, status: i === 1 && tick % 9 < 2 ? 'review' : t.status })),
    workers: DEMO_WORKERS.map((w) => ({ ...w, last_seen: now })),
    messages: DEMO_MESSAGES.map((m) => ({ ...m, ts: now - 60 })),
  };
}
