"""
Legacy passive-monitor support (v1 protocol) + the ported "Mistral brain".

    WS /ws         reporters push one status message per laptop per second (shared/schema.json)
    WS /view       3D/AR viewers get {"type":"state","ts","team_summary","alerts","laptops"} at 2 Hz
    POST /summarize  one raw agent log line -> a short task title (max 6 words) via Mistral Small

The hub merges these reporter laptops with one synthesized entry per connected orchestra
worker (see Hub.legacy_laptops) so web/ar.html also shows the agents the orchestra runs.

LegacyState keeps the latest message per laptop, keyed by laptop_key() (tailscale_ip, falling
back to the laptop hostname) and pruned by "_seen" — the server receive time — so a laptop
with a wrong clock still shows up in the view.

LegacyBrain is the rate-limit-safe Mistral layer for the 3D view:
  - summaries are cached per (agent name, log line); at most one Mistral call per agent every
    SUMMARY_EVERY s and at most MAX_CONCURRENT calls in flight, all as background asyncio tasks,
    so ingestion never waits for Mistral; until a summary is ready the agent keeps its previous
    summary (or "" — never the raw log line); no API key or a failed call -> "", not cached
  - every agent gets a "state": "error" (error/failed/traceback/exception/tests failing/
    exit code N), "stuck" if it keeps erroring for STUCK_AFTER s, "working" if cpu > WORKING_CPU
    or its log line changed in the last WORKING_WINDOW s, otherwise "idle"
  - collision alerts when 2+ different agents edit the same file within COLLISION_WINDOW s
  - one team headline every TEAM_SUMMARY_EVERY s, and only when the agents/alerts text changed
    (no API key or error -> plain count line)
"""
from __future__ import annotations

import asyncio
import copy
import os
import re
import time

import httpx

SUMMARY_EVERY = float(os.environ.get("SUMMARY_EVERY", "5"))
MAX_CONCURRENT = 2          # Mistral calls in flight at once
CACHE_MAX = 2000            # distinct log lines kept in the summary cache
WORKING_CPU = 5.0           # % CPU above which an agent counts as working
WORKING_WINDOW = 10.0       # seconds a new log line keeps an agent "working"
STUCK_AFTER = float(os.environ.get("STUCK_AFTER", "45"))   # seconds of errors -> "stuck"
COLLISION_WINDOW = 60.0     # seconds an edit counts towards a collision
TEAM_SUMMARY_EVERY = 10.0   # seconds between team-headline refreshes

ERROR_RE = re.compile(
    r"\b(error|failed|failure|traceback|exception|tests? failing|exit (?:code )?[1-9]\d*)\b",
    re.IGNORECASE,
)
EDIT_RE = re.compile(
    r"\b(edit|edited|editing|write|wrote|writing|patch|patched|modif|updat|creat|apply|applied)",
    re.IGNORECASE,
)
FILE_RE = re.compile(
    r"[\w./-]*[\w-]+\.(?:py|js|ts|tsx|jsx|html|css|json|toml|md|go|rs|java|c|cpp|h|yml|yaml|sh)\b"
)

SUMMARY_SYSTEM = (
    "From one log line of an AI coding agent, name the task it is "
    "working on as a short title (max 6 words), like a chat title. "
    "No preamble, no quotes, no emoji, no file paths or IDs."
)
HEADLINE_SYSTEM = (
    "You are the team lead watching AI coding agents on several laptops. "
    "Write ONE short headline (max 15 words) about how the team is doing. "
    "Mention anything stuck or colliding first. No preamble, no emoji."
)


def laptop_key(data: dict) -> str:
    """Tailscale IP, falling back to the laptop hostname when the IP is empty or loopback."""
    key = str(data.get("tailscale_ip") or "").strip()
    if not key or key == "127.0.0.1":
        key = str(data.get("laptop") or "").strip() or "unknown"
    return key


class LegacyState:
    """Latest reporter status per laptop (key = laptop_key(data))."""

    def __init__(self, stale_after: float) -> None:
        self.stale_after = stale_after
        self.states: dict[str, dict] = {}

    @staticmethod
    def _seen_of(state: dict) -> float:
        # server receive time; fall back to the reporter's ts for data ingested without one
        seen = state.get("_seen")
        if seen is None:
            seen = state.get("ts", 0)
        return float(seen or 0)

    def ingest(self, data: dict, t: float | None = None) -> None:
        """Store one reporter message under laptop_key, stamped with the server receive time."""
        if t is None:
            t = time.time()
        data.setdefault("ts", t)
        data["_seen"] = t
        self.states[laptop_key(data)] = data

    def prune(self, now: float) -> None:
        for key in [k for k, s in self.states.items() if now - self._seen_of(s) > self.stale_after]:
            del self.states[key]

    def fresh(self, now: float) -> list[dict]:
        """Deep copies of all non-stale laptop states (safe to merge into)."""
        return [copy.deepcopy(s) for s in self.states.values() if now - self._seen_of(s) <= self.stale_after]


async def mistral_chat(system: str, user: str, api_key: str, model: str) -> str:
    """One Mistral chat call. Raises on error (callers decide how to fall back)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
            },
        )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def summarize(last_log: str, api_key: str, model: str) -> str:
    """
    Turn one raw agent log line into a short task title (max 6 words, like a
    chat title) using Mistral Small. Without MISTRAL_API_KEY (or on any API
    error) it returns "" — the UI then shows the reporter's session title or
    just the agent name.
    """
    line = (last_log or "").strip()
    if not line:
        return ""
    if not api_key:
        return ""
    try:
        return await mistral_chat(SUMMARY_SYSTEM, line[:500], api_key, model)
    except Exception as exc:  # never let summarization break ingestion
        print(f"[summarize] fallback after error: {exc}")
        return ""


def count_line(laptops: list[dict]) -> str:
    """Plain team headline without Mistral, e.g. '3 working, 1 stuck across 2 laptop(s)'."""
    counts: dict[str, int] = {}
    for st in laptops:
        for a in st.get("agents") or []:
            state = a.get("state", "idle")
            counts[state] = counts.get(state, 0) + 1
    if not counts:
        return "No agents running"
    order = ["working", "stuck", "error", "idle"]
    parts = [f"{counts[k]} {k}" for k in order if counts.get(k)]
    return ", ".join(parts) + f" across {len(laptops)} laptop(s)"


class LegacyBrain:
    """Rate-limit-safe Mistral layer for the 3D view (see module docstring)."""

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self.sem = asyncio.Semaphore(MAX_CONCURRENT)
        self.summary_cache: dict[tuple[str, str], str] = {}     # (agent name, log line) -> summary
        self.in_flight: set[tuple[str, str]] = set()             # lines being summarised right now
        self.last_call: dict[str, float] = {}                    # agent key -> last Mistral call
        self.last_summary: dict[str, str] = {}                   # agent key -> latest summary shown
        self.last_log_seen: dict[str, tuple[str, float]] = {}    # agent key -> (line, changed at)
        self.error_since: dict[str, float] = {}                  # agent key -> when its errors started
        self.recent_edits: dict[str, dict[str, float]] = {}      # agent key -> {file: last edit}
        self.team_summary: str = ""

    @staticmethod
    def _agent_key(lkey: str, agent: dict) -> str:
        return f"{lkey}:{agent.get('name', '')}:{agent.get('pid')}"

    # ---------------------------------------------------------------- summaries
    async def _summarize_line(self, akey: str, name: str, line: str) -> None:
        """Summarise one line as a background task; cache it on success only."""
        ck = (name, line)
        try:
            async with self.sem:
                summary = await mistral_chat(SUMMARY_SYSTEM, line[:500], self.api_key, self.model)
            if len(self.summary_cache) >= CACHE_MAX:
                self.summary_cache.clear()
            self.summary_cache[ck] = summary
        except Exception as exc:
            print(f"[brain] summary fallback after error: {exc}")
            summary = ""  # never the raw line; not cached, so it is retried after SUMMARY_EVERY
        finally:
            self.in_flight.discard(ck)
        self.last_summary[akey] = summary

    # ---------------------------------------------------------------- state
    def _track_log(self, akey: str, line: str, now: float) -> float:
        prev = self.last_log_seen.get(akey)
        if prev is None or prev[0] != line:
            self.last_log_seen[akey] = (line, now)
        return self.last_log_seen[akey][1]

    def _compute_state(self, akey: str, agent: dict, line: str, changed_at: float, now: float) -> str:
        if line and ERROR_RE.search(line):
            self.error_since.setdefault(akey, now)
            return "stuck" if now - self.error_since[akey] >= STUCK_AFTER else "error"
        self.error_since.pop(akey, None)
        if float(agent.get("cpu") or 0) > WORKING_CPU or now - changed_at < WORKING_WINDOW:
            return "working"
        return "idle"

    def _record_edits(self, akey: str, line: str, now: float) -> None:
        if line and EDIT_RE.search(line):
            for f in FILE_RE.findall(line):
                self.recent_edits.setdefault(akey, {})[f.lstrip("./")] = now

    def _fill(self, lkey: str, agent: dict, now: float, *, schedule: bool) -> None:
        """Set summary + state (+ agent_id, edits) on one reporter agent; never awaits Mistral."""
        akey = self._agent_key(lkey, agent)
        agent["agent_id"] = akey
        name = str(agent.get("name", ""))
        line = (agent.get("last_log") or "").strip()
        changed_at = self._track_log(akey, line, now)
        if schedule:
            if agent.get("summary"):
                pass  # reporter already sent one
            elif not line:
                agent["summary"] = ""
            elif (name, line) in self.summary_cache:
                agent["summary"] = self.summary_cache[(name, line)]
                self.last_summary[akey] = agent["summary"]
            else:
                agent["summary"] = self.last_summary.get(akey) or ""
                ck = (name, line)
                last = self.last_call.get(akey)
                if self.api_key and ck not in self.in_flight and (last is None or now - last >= SUMMARY_EVERY):
                    self.in_flight.add(ck)
                    self.last_call[akey] = now
                    asyncio.create_task(self._summarize_line(akey, name, line))
            self._record_edits(akey, line, now)
        agent["state"] = self._compute_state(akey, agent, line, changed_at, now)

    def on_status(self, laptop_key: str, data: dict, now: float) -> None:
        """Fill summaries + state for one reporter message (Mistral runs in background tasks)."""
        agents = data.get("agents")
        if not isinstance(agents, list):
            return
        for agent in agents:
            if isinstance(agent, dict):
                self._fill(laptop_key, agent, now, schedule=True)

    def annotate(self, laptops: list[dict], now: float) -> None:
        """Swap finished summaries into the laptop copies and add state to agents that have
        none (the synthesized orchestra worker agents, whose own summary is kept as-is)."""
        for st in laptops:
            lkey = laptop_key(st)
            for agent in st.get("agents") or []:
                if not isinstance(agent, dict):
                    continue
                akey = agent.get("agent_id") or self._agent_key(lkey, agent)
                agent["agent_id"] = akey
                name = str(agent.get("name", ""))
                line = (agent.get("last_log") or "").strip()
                if not str(akey).startswith("task:"):  # never overwrite a synthesized worker summary
                    cached = self.summary_cache.get((name, line))
                    if cached:
                        agent["summary"] = cached
                        self.last_summary[akey] = cached
                if not agent.get("state"):
                    changed_at = self._track_log(akey, line, now)
                    agent["state"] = self._compute_state(akey, agent, line, changed_at, now)

    # ---------------------------------------------------------------- collisions
    def collisions(self, laptops: list[dict], now: float) -> list[dict]:
        """Files edited by 2+ different agents within COLLISION_WINDOW."""
        live: dict[str, tuple[str, str]] = {}
        for st in laptops:
            for a in st.get("agents") or []:
                live[a.get("agent_id", "")] = (st.get("owner") or st.get("laptop") or "?", a.get("name", "?"))
        by_file: dict[str, list[str]] = {}
        for akey, files in self.recent_edits.items():
            if akey not in live:
                continue
            for f, t in files.items():
                if now - t <= COLLISION_WINDOW:
                    by_file.setdefault(f, []).append(akey)
        alerts = []
        for f, akeys in sorted(by_file.items()):
            if len(akeys) >= 2:
                who = " and ".join(f"{live[k][1]} ({live[k][0]})" for k in akeys)
                alerts.append({"kind": "collision", "file": f, "agents": akeys,
                               "text": f"{who} are both editing {f}"})
        return alerts

    # ---------------------------------------------------------------- team headline
    async def team_summary_loop(self, snapshot_fn) -> None:
        """Every TEAM_SUMMARY_EVERY s ask Mistral for ONE team headline — but only when the
        agents/alerts text changed. No API key or error -> count_line."""
        last_input = ""
        while True:
            await asyncio.sleep(TEAM_SUMMARY_EVERY)
            try:
                snap = await snapshot_fn()
                laptops = snap.get("laptops", []) if isinstance(snap, dict) else []
                if not self.api_key or not laptops:
                    self.team_summary = count_line(laptops)
                    continue
                lines = [
                    f"{st.get('owner') or st.get('laptop')}'s {a.get('name')}: {a.get('state')} - {a.get('summary')}"
                    for st in laptops for a in (st.get("agents") or [])
                ]
                lines += [f"ALERT: {al.get('text')}" for al in snap.get("alerts", [])]
                text = "\n".join(lines)
                if text == last_input:
                    continue  # nothing changed, no API call
                last_input = text
                try:
                    async with self.sem:
                        self.team_summary = await mistral_chat(HEADLINE_SYSTEM, text[:3000],
                                                              self.api_key, self.model)
                except Exception as exc:
                    print(f"[team_summary] fallback after error: {exc}")
                    self.team_summary = count_line(laptops)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[team_summary] error: {exc}")
