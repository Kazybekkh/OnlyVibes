// Live data hook: WS /view (2 msg/s broadcast) with REST /api/state polling
// fallback, plus a built-in demo mode when no server is reachable/configured.
import { useEffect, useRef, useState } from 'react';
import { demoSnapshot } from './demo';
import { normalizeUrl } from './server';
import type { Snapshot } from './types';

export type ConnStatus = 'live' | 'polling' | 'demo' | 'connecting' | 'offline';

export interface LiveState {
  snapshot: Snapshot | null;
  status: ConnStatus;
  /** epoch ms of last received frame, for the "updated Xs ago" caption */
  lastSeen: number | null;
}

export function useOnlyVibes(url: string, demo: boolean, nonce: number): LiveState {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [status, setStatus] = useState<ConnStatus>('connecting');
  const [lastSeen, setLastSeen] = useState<number | null>(null);
  const demoTick = useRef(0);

  useEffect(() => {
    if (demo) {
      const push = () => {
        demoTick.current += 1;
        setSnapshot(demoSnapshot(demoTick.current));
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
      setSnapshot(s);
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
      pollTimer = setInterval(tick, 1000);
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
        if (!gotLive) {
          startPolling();
        } else {
          setStatus('connecting');
          reconnectTimer = setTimeout(connect, 2000);
        }
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (pollTimer) clearInterval(pollTimer);
      try { ws?.close(); } catch { /* already closed */ }
    };
  }, [url, demo, nonce]);

  const derived: ConnStatus = !normalizeUrl(url) ? 'offline' : demo ? 'demo' : status;
  return { snapshot, status: derived, lastSeen };
}
