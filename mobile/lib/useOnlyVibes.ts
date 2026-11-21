// Live data hook for the orchestra hub (shared/PROTOCOL.md v2):
//   WS  /view      -> {type:"state", ts, laptops} at 2 Hz   (laptop fleet)
//   GET /api/state -> {ts, projects, tasks, workers, messages, laptops} (board)
// merged into one Snapshot. Falls back to REST-only polling when the WS can't
// connect, and demo mode runs fully offline.
import { useEffect, useMemo, useRef, useState } from 'react';
import { demoSnapshot } from './demo';
import { normalizeUrl } from './server';
import { agentBase, type Agent, type AgentState, type Laptop, type Snapshot } from './types';

export type ConnStatus = 'live' | 'polling' | 'demo' | 'connecting' | 'offline';

export interface LiveState {
  snapshot: Snapshot | null;
  status: ConnStatus;
  /** epoch ms of last received frame, for the "updated Xs ago" caption */
  lastSeen: number | null;
}

// ---------------------------------------------------------------------------
// client-side agent state derivation (orchestra servers send raw agents)
const ERROR_RE = /\b(error|failed|exception|traceback|fatal)\b/i;
const WORKING_CPU = 15;
const WORKING_WINDOW_MS = 4000;
const STUCK_AFTER_MS = 20000;

interface AgentTrack { line: string; changedAt: number; errorSince: number | null }

function deriveAgentState(
  agent: Agent,
  track: AgentTrack | undefined,
  nowMs: number,
): { state: AgentState; track: AgentTrack } {
  const line = (agent.last_log || '').trim();
  let t: AgentTrack = track ?? { line, changedAt: nowMs, errorSince: null };
  if (t.line !== line) t = { ...t, line, changedAt: nowMs };
  if (line && ERROR_RE.test(line)) {
    const errorSince = t.errorSince ?? nowMs;
    t = { ...t, errorSince };
    return { state: nowMs - errorSince >= STUCK_AFTER_MS ? 'stuck' : 'error', track: t };
  }
  t = { ...t, errorSince: null };
  if ((agent.cpu || 0) > WORKING_CPU || nowMs - t.changedAt < WORKING_WINDOW_MS) {
    return { state: 'working', track: t };
  }
  return { state: 'idle', track: t };
}

function deriveTeamSummary(laptops: Laptop[]): string {
  const counts: Record<string, number> = {};
  for (const l of laptops) for (const a of l.agents ?? []) {
    counts[a.state ?? 'idle'] = (counts[a.state ?? 'idle'] ?? 0) + 1;
  }
  const total = Object.values(counts).reduce((s, n) => s + n, 0);
  if (!total) return 'No agents running';
  const order: AgentState[] = ['working', 'stuck', 'error', 'idle'];
  const parts = order.filter((k) => counts[k]).map((k) => `${counts[k]} ${k}`);
  return `${parts.join(', ')} across ${laptops.length} laptop(s)`;
}

/** attach derived state to every agent (mutates a copy of the snapshot) */
function enrich(snapshot: Snapshot, tracks: Map<string, AgentTrack>, nowMs: number): Snapshot {
  const laptops = (snapshot.laptops ?? []).map((l: Laptop) => ({
    ...l,
    agents: (l.agents ?? []).map((a: Agent) => {
      const key = `${l.tailscale_ip || l.laptop}:${a.name}:${a.pid}`;
      const { state, track } = deriveAgentState(a, tracks.get(key), nowMs);
      tracks.set(key, track);
      return { ...a, state, agent_id: a.agent_id ?? key };
    }),
  }));
  return {
    ...snapshot,
    laptops,
    team_summary: snapshot.team_summary || deriveTeamSummary(laptops),
  };
}

// ---------------------------------------------------------------------------

export function useOnlyVibes(url: string, demo: boolean, nonce: number): LiveState {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [status, setStatus] = useState<ConnStatus>('connecting');
  const [lastSeen, setLastSeen] = useState<number | null>(null);
  // auto-demo: if the hub is unreachable for a few seconds, show the fake
  // fleet so the app never renders empty (status pill shows DEMO)
  const [autoDemo, setAutoDemo] = useState(false);
  const demoTick = useRef(0);
  const tracks = useRef(new Map<string, AgentTrack>());
  // reset auto-demo when the target changes (render-phase adjust, per React docs)
  const attemptKey = `${nonce}:${url}`;
  const [prevKey, setPrevKey] = useState(attemptKey);
  if (prevKey !== attemptKey) {
    setPrevKey(attemptKey);
    setAutoDemo(false);
  }

  useEffect(() => {
    if (demo || autoDemo || snapshot) return;
    const t = setTimeout(() => {
      if (!snapshot) setAutoDemo(true);
    }, 5000);
    return () => clearTimeout(t);
  }, [demo, autoDemo, snapshot]);

  const effectiveDemo = demo || autoDemo;

  useEffect(() => {
    if (effectiveDemo) {
      const push = () => {
        demoTick.current += 1;
        setSnapshot(enrich(demoSnapshot(demoTick.current), tracks.current, Date.now()));
        setLastSeen(Date.now());
      };
      push();
      const t = setInterval(push, 1000);
      return () => clearInterval(t);
    }

    const norm = normalizeUrl(url);
    if (!norm) return; // invalid URL -> derived 'offline' status below

    let cancelled = false;
    let ws: WebSocket | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let gotLive = false;

    const ingest = (s: Snapshot) => {
      setSnapshot((prev) => {
        // merge: WS frames carry laptops, REST frames carry the board fields
        const merged: Snapshot = prev && s.laptops.length === 0 && prev.laptops.length > 0 && !s.projects
          ? { ...s, laptops: prev.laptops }
          : { ...prev, ...s, laptops: s.laptops ?? prev?.laptops ?? [] };
        return enrich(merged, tracks.current, Date.now());
      });
      setLastSeen(Date.now());
    };

    const startPolling = () => {
      if (cancelled || pollTimer) return;
      setStatus('polling');
      const tick = async () => {
        try {
          const r = await fetch(`${norm.http}/api/state`);
          if (r.ok) ingest((await r.json()) as Snapshot);
        } catch { /* keep polling */ }
      };
      void tick();
      pollTimer = setInterval(tick, 2000);
    };

    const connect = () => {
      if (cancelled) return;
      setStatus((s) => (s === 'live' ? s : 'connecting'));
      try {
        ws = new WebSocket(`${norm.ws}/view`);
      } catch {
        startPolling();
        return;
      }
      ws.onmessage = (ev) => {
        try {
          const s = JSON.parse(ev.data as string) as Snapshot;
          if (s && s.type === 'state') {
            gotLive = true;
            setStatus('live');
            ingest(s);
          }
        } catch { /* ignore bad frame */ }
      };
      ws.onerror = () => {
        if (!gotLive) startPolling();
      };
      ws.onclose = () => {
        if (cancelled) return;
        if (!gotLive) startPolling();
        else {
          setStatus('connecting');
          reconnectTimer = setTimeout(connect, 2000);
        }
      };
    };

    connect();
    startPolling(); // board data (projects/tasks/workers/messages) always via REST

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (pollTimer) clearInterval(pollTimer);
      try { ws?.close(); } catch { /* already closed */ }
    };
  }, [url, effectiveDemo, nonce]);

  const derived: ConnStatus = !normalizeUrl(url) ? 'offline' : effectiveDemo ? 'demo' : status;
  return useMemo(
    () => ({ snapshot, status: derived, lastSeen }),
    [snapshot, derived, lastSeen],
  );
}

export { agentBase };
