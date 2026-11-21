// Mirrors shared/schema.json (reporter status) + server/main.py snapshot()

export type AgentName = 'vibe' | 'claude-code' | 'codex' | 'cursor';

export type AgentState = 'working' | 'stuck' | 'error' | 'idle';

export interface Agent {
  name: AgentName | string;
  pid: number;
  cpu: number;
  cwd: string;
  last_log: string;
  summary: string;
  /** filled in by server fill_agent() */
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
  team_summary: string;
  alerts: Alert[];
  laptops: Laptop[];
  voice?: VoiceTurn;
  voice_config?: Record<string, unknown>;
}

export const laptopKey = (l: Laptop) => l.tailscale_ip || l.laptop;
