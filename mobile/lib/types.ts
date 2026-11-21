// Mirrors shared/schema.json (reporter status) + shared/PROTOCOL.md v2
// (orchestra: projects/tasks/workers/messages) + server /view & /api/state frames.

export type AgentName = 'vibe' | 'claude-code' | 'codex' | 'cursor';

export type AgentState = 'working' | 'stuck' | 'error' | 'idle';

export interface Agent {
  /** display id; base agent id plus session info, e.g. "claude-code · OnlyVibes › Explore" */
  name: string;
  pid: number;
  cpu: number;
  cwd: string;
  last_log: string;
  summary: string;
  /** schema.json: app | session | subagent; orchestra worker tasks use "task" */
  kind?: 'app' | 'session' | 'subagent' | 'task' | string;
  task_id?: string;
  task?: string;
  /** server-enriched on old servers; derived client-side on orchestra servers */
  state?: AgentState;
  agent_id?: string;
}

export interface Laptop {
  laptop: string;
  owner: string;
  tailscale_ip: string;
  ts: number;
  agents: Agent[];
}

// ---- orchestra entities (shared/PROTOCOL.md §1) ----

export type ProjectStatus = 'planning' | 'active' | 'done' | 'failed';

export interface Project {
  id: string;
  prompt: string;
  title: string;
  repo: string;
  base_branch: string;
  status: ProjectStatus | string;
  created_at: number;
  plan_task_id: string;
  summary: string;
}

export type TaskStatus =
  | 'backlog' | 'todo' | 'claimed' | 'in_progress' | 'review' | 'done' | 'failed' | 'cancelled';

export interface Assignee {
  worker_id: string;
  laptop: string;
  owner: string;
  agent: string;
}

export interface Task {
  id: string;
  key: string;
  project_id: string;
  kind: 'plan' | 'build' | string;
  title: string;
  status: TaskStatus | string;
  assignee: Assignee | null;
  progress: number;
  progress_note: string;
  last_event: string;
  priority: number;
  branch: string;
  pr_url: string;
  error?: string;
  result?: string;
  updated_at?: number;
}

export interface Worker {
  id: string;
  laptop: string;
  owner: string;
  tailscale_ip: string;
  agents: string[];
  max_concurrent: number;
  running: string[];
  status: 'idle' | 'busy' | 'offline' | string;
  last_seen: number;
}

export interface Message {
  id: string;
  project_id: string;
  ts: number;
  from: { kind: 'agent' | 'human' | 'system' | string; name?: string; agent?: string; worker_id?: string; task_id?: string };
  to: string;
  text: string;
  reply_to: string | null;
}

// ---- legacy extras (pre-orchestra servers) ----

export interface Alert {
  kind: string;
  file?: string;
  agents?: string[];
  text: string;
}

export interface VoiceTurn {
  id: number | string;
  client_id?: string;
  heard?: string;
  reply?: string;
  state?: string;
  source_role?: string;
  speak_on?: string[];
  [k: string]: unknown;
}

export interface Snapshot {
  type: string;
  ts: number;
  laptops: Laptop[];
  /** present when merged from GET /api/state (orchestra hub) */
  projects?: Project[];
  tasks?: Task[];
  workers?: Worker[];
  messages?: Message[];
  /** legacy servers only */
  team_summary?: string;
  alerts?: Alert[];
  voice?: VoiceTurn;
  voice_config?: Record<string, unknown>;
}

export const laptopKey = (l: Laptop) => l.tailscale_ip || l.laptop;

/** base agent id from a display name: "claude-code · OnlyVibes › Explore" -> "claude-code" */
export const agentBase = (name: string) => name.split(/[ ·›#]/)[0].trim();
