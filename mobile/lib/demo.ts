// Standalone demo data — mirrors the FAKE fallback in web/index.html so the app
// is fully usable with no server running.
import type { Snapshot } from './types';

const FAKE = [
  { laptop: 'kay-mbp', owner: 'kay', tailscale_ip: '100.64.0.11',
    agents: [{ name: 'vibe', pid: 3001, cpu: 42.1, cwd: '/Users/kay/OnlyVibes', last_log: '⏺ Edit server/main.py', summary: 'editing the FastAPI server' },
             { name: 'cursor', pid: 3018, cpu: 12.4, cwd: '/Users/kay/OnlyVibes', last_log: 'Agent: web/index.html', summary: 'tweaking the 3D view' }] },
  { laptop: 'amina-air', owner: 'amina', tailscale_ip: '100.64.0.27',
    agents: [{ name: 'claude-code', pid: 4102, cpu: 65.0, cwd: '/Users/amina/agent-app', last_log: 'Read reporter/reporter.py', summary: 'reading the reporter code' },
             { name: 'codex', pid: 4117, cpu: 30.7, cwd: '/Users/amina/agent-app', last_log: 'patched 3 files', summary: 'patching three files' }] },
  { laptop: 'dao-legion', owner: 'dao', tailscale_ip: '100.64.0.42',
    agents: [{ name: 'codex', pid: 5201, cpu: 8.2, cwd: '/Users/dao/xr', last_log: 'reasoning…', summary: 'thinking about WebXR' },
             { name: 'vibe', pid: 5233, cpu: 51.9, cwd: '/Users/dao/xr', last_log: '⏺ Ran pytest — 12 passed', summary: 'running the test suite' },
             { name: 'claude-code', pid: 5250, cpu: 3.3, cwd: '/Users/dao/xr', last_log: 'TodoWrite 3/5', summary: 'updating the todo list' }] },
];

const STATES = ['working', 'idle', 'working', 'working', 'idle', 'stuck', 'error'] as const;

export function demoSnapshot(tick: number): Snapshot {
  const now = Date.now() / 1000;
  return {
    type: 'state',
    ts: now,
    team_summary: '6 agents across 3 laptops — codex on dao-legion is stuck on a merge conflict',
    alerts: tick % 7 === 0
      ? [{ kind: 'collision', file: 'server/main.py', text: 'vibe (kay) and codex (dao) are both editing server/main.py' }]
      : [],
    laptops: FAKE.map((l) => ({
      ...l,
      ts: now,
      agents: l.agents.map((a, i) => ({
        ...a,
        cpu: Math.max(1, a.cpu + Math.sin(tick + a.pid) * 8),
        state: STATES[(tick + i) % STATES.length],
        agent_id: `${l.tailscale_ip}:${a.name}:${a.pid}`,
      })),
    })),
  };
}
