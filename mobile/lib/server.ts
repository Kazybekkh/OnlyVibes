// Server connection config: module-level store with useSyncExternalStore so any
// screen can read/update it and the live hook re-connects on change.
import { useSyncExternalStore } from 'react';

export interface ServerConfig {
  /** what the user typed, e.g. http://100.64.0.11:8000 or ws://localhost:8000 */
  url: string;
  demo: boolean;
  /** bumped by "Reconnect" to force the hook to re-run */
  nonce: number;
}

const DEFAULT_URL =
  (process.env.EXPO_PUBLIC_SERVER_URL as string | undefined) ?? 'http://localhost:8000';

let config: ServerConfig = { url: DEFAULT_URL, demo: false, nonce: 0 };
const listeners = new Set<() => void>();

export function getServerConfig(): ServerConfig {
  return config;
}

export function setServerConfig(patch: Partial<ServerConfig>) {
  config = { ...config, ...patch };
  listeners.forEach((l) => l());
}

export function useServerConfig(): ServerConfig {
  return useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => listeners.delete(cb); },
    getServerConfig,
  );
}

export interface NormalizedUrl {
  http: string; // REST base, e.g. http://100.64.0.11:8000
  ws: string;   // WS base,   e.g. ws://100.64.0.11:8000
}

export function normalizeUrl(raw: string): NormalizedUrl | null {
  const s = raw.trim().replace(/\/+$/, '');
  if (!s) return null;
  if (s.startsWith('ws://')) return { http: s.replace(/^ws:\/\//, 'http://'), ws: s };
  if (s.startsWith('wss://')) return { http: s.replace(/^wss:\/\//, 'https://'), ws: s };
  if (s.startsWith('http://')) return { http: s, ws: s.replace(/^http:\/\//, 'ws://') };
  if (s.startsWith('https://')) return { http: s, ws: s.replace(/^https:\/\//, 'wss://') };
  // bare host[:port] — assume http/ws
  return { http: `http://${s}`, ws: `ws://${s}` };
}
