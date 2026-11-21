"""
The orchestra hub: every state transition of PROTOCOL.md §1-§5 lives here.

Concurrency model: one asyncio.Lock (`hub.lock`) guards all state. Methods whose name starts
with an underscore, and every `worker_*`, `human_*`, `agent_*` method, must be called with the
lock held; they are synchronous so a transition can never interleave with another one.
Outgoing frames are queued on per-socket Peers while the lock is held (peers.py), so the
order browsers / workers see always matches the order state changed.

Background loops (started by `start()` from the FastAPI lifespan):
    scheduler   every tick_interval s, or immediately when kicked: offline detection,
                claim timeouts, backlog promotion, assignment of todo tasks to free slots
    saver       debounced atomic persistence to data/state.json (+ data/events/*.json)
    pinger      {"type":"ping"} to /board clients every ping_interval s
    flusher     coalesced "task" frames for tasks whose transcript grew
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import traceback
from dataclasses import dataclass

from config import Settings
from legacy import LegacyBrain, LegacyState, count_line
from models import (
    ACTIVE_STATUSES,
    EVENT_KINDS,
    MAX_EVENT_DATA,
    MAX_MESSAGE_TEXT,
    SATISFIED_DEP_STATUSES,
    SERVER_VERSION,
    ApiError,
    agent_board_entry,
    board_entry,
    clamp_int,
    default_title,
    message_id,
    message_num,
    new_task,
    new_token,
    normalize_repo,
    now,
    one_line,
    str_list,
    task_number,
    truncate,
)
from peers import Peer
from planning import PlanError, map_plan, normalize_plan
from scheduler import WorkerSlot, schedule
from store import Store

MAX_STATE_MESSAGES = 300   # messages included in /api/state and the /board snapshot
MAX_INBOX_BATCH = 200
CANCEL_SETTLE = 3.0        # seconds a claim-timed-out task waits before it can be dispatched again


@dataclass
class WorkerConn:
    worker_id: str
    peer: Peer
    http_base: str
    connected_at: float


class Hub:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.store = Store(settings.data_dir)
        self.legacy = LegacyState(settings.stale_after)
        self.brain = LegacyBrain(settings.mistral_api_key, settings.mistral_model)
        self.lock = asyncio.Lock()
        self.conns: dict[str, WorkerConn] = {}
        self.board_peers: set[Peer] = set()
        self._kick = asyncio.Event()
        self._save_needed = asyncio.Event()
        self._msg_event = asyncio.Event()   # replaced (after set) on every new message
        self._pending_task_frames: set[str] = set()
        self._warned: set[str] = set()
        self._loops: list[asyncio.Task] = []

    # ================================================================ lifecycle
    def load(self) -> None:
        """Load persisted state; running tasks go back to todo (their workers will re-hello)."""
        if not self.store.load():
            return
        t = now()
        for worker in self.store.workers.values():
            worker["running"] = []
            worker["status"] = "offline"
        # Workers retry with backoff, so after a restart give them restart_grace (not just
        # reattach_grace) to re-hello and re-attach what they still run / finished meanwhile.
        # That covers tasks running at the crash and tasks requeued by the disconnects of a
        # graceful shutdown (todo with a hold), which would otherwise go to whoever is first.
        grace = max(self.s.restart_grace, self.s.reattach_grace)
        for task in self.store.tasks.values():
            meta = self.store.meta_of(task["id"])
            if task["status"] in ("claimed", "in_progress"):
                was = task["status"]
                task["status"] = "todo"
                task["assignee"] = None
                task["updated_at"] = t
                meta["hold_until"] = t + grace
                self._event(task, "system", f"hub restarted while this task was {was}; back to todo "
                                            f"(its worker may re-attach within {grace:.0f}s)")
            elif task["status"] == "todo" and meta.get("hold_until"):
                meta["hold_until"] = t + grace
        for pid in self.store.projects:
            self._refresh_project(pid, broadcast=False)
        print(f"[hub] loaded {len(self.store.projects)} projects, {len(self.store.tasks)} tasks "
              f"from {self.store.data_dir}")

    async def start(self) -> None:
        self.load()

        async def _brain_snapshot() -> dict:
            """What brain.team_summary_loop asks Mistral about (takes the lock itself)."""
            async with self.lock:
                return self.legacy_view(now())

        self._loops = [
            asyncio.create_task(self._scheduler_loop(), name="ov-scheduler"),
            asyncio.create_task(self._saver_loop(), name="ov-saver"),
            asyncio.create_task(self._ping_loop(), name="ov-ping"),
            asyncio.create_task(self._flush_loop(), name="ov-flush"),
            asyncio.create_task(self.brain.team_summary_loop(_brain_snapshot), name="ov-team-summary"),
        ]

    async def stop(self) -> None:
        for loop in self._loops:
            loop.cancel()
        for loop in self._loops:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await loop
        self._loops = []
        async with self.lock:
            for conn in self.conns.values():
                conn.peer.close(1001)
            for peer in self.board_peers:
                peer.close(1001)
            payload = self.store.serialize() if self.s.persist else None
        if payload is not None:
            await asyncio.to_thread(self.store.write, payload)

    async def _scheduler_loop(self) -> None:
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._kick.wait(), timeout=self.s.tick_interval)
            self._kick.clear()
            try:
                async with self.lock:
                    self.tick(now())
            except Exception:
                traceback.print_exc()
                await asyncio.sleep(self.s.tick_interval)

    async def _saver_loop(self) -> None:
        while True:
            await self._save_needed.wait()
            await asyncio.sleep(self.s.save_debounce)
            self._save_needed.clear()
            if not self.s.persist:
                continue
            try:
                async with self.lock:
                    payload = self.store.serialize()
                await asyncio.to_thread(self.store.write, payload)
            except Exception:
                traceback.print_exc()

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.ping_interval)
            async with self.lock:
                self._broadcast({"type": "ping", "ts": now()})

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.frame_flush_interval)
            if not self._pending_task_frames:
                continue
            async with self.lock:
                pending, self._pending_task_frames = self._pending_task_frames, set()
                for tid in pending:
                    task = self.store.tasks.get(tid)
                    if task is not None:
                        self._broadcast({"type": "task", "task": task})

    # ============================================================ low-level helpers
    def _changed(self, *, kick: bool = True) -> None:
        """Something persistent changed: schedule a save and (usually) kick the scheduler."""
        self.store.dirty = True
        self._save_needed.set()
        if kick:
            self._kick.set()

    def _broadcast(self, frame: dict) -> None:
        if not self.board_peers:
            return
        text = json.dumps(frame, ensure_ascii=False)
        for peer in list(self.board_peers):
            if not peer.send_text(text):
                self.board_peers.discard(peer)

    def _touch(self, task: dict, t: float | None = None) -> None:
        """Task fields changed: bump updated_at, broadcast it now, persist."""
        task["updated_at"] = now() if t is None else t
        self._pending_task_frames.discard(task["id"])
        self._broadcast({"type": "task", "task": task})
        self._changed()

    def _event(self, task: dict, kind: str, text: str, *, tool: str = "", data: dict | None = None,
               ts: float | None = None) -> dict:
        """Append one transcript event (seq assigned here) and broadcast it."""
        ev: dict = {"ts": now() if ts is None else ts, "kind": kind, "text": truncate(text)}
        if tool:
            ev["tool"] = tool
        if data:
            ev["data"] = data
        stored = self.store.append_event(task["id"], ev)
        task["event_count"] = len(self.store.events[task["id"]])
        line = one_line(stored["text"])
        if line:
            task["last_event"] = line
        self._broadcast({"type": "event", "task_id": task["id"], "event": stored})
        self._changed(kick=False)
        return stored

    def warn_once(self, key: str, text: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            print(f"[hub] {text}")

    def _task(self, task_id: str) -> dict:
        task = self.store.tasks.get(task_id)
        if task is None:
            raise ApiError(404, "task_not_found", f"no task {task_id!r}")
        return task

    def _project(self, project_id: str) -> dict:
        project = self.store.projects.get(project_id)
        if project is None:
            raise ApiError(404, "project_not_found", f"no project {project_id!r}")
        return project

    # ------------------------------------------------------------ project status
    def _compute_project_status(self, project: dict) -> str:
        tasks = self.store.project_tasks(project["id"])
        plan = self.store.tasks.get(project["plan_task_id"])
        builds = [t for t in tasks if t["kind"] == "build"]
        plan_status = plan["status"] if plan else "done"
        if plan_status == "failed":
            return "failed"
        if plan_status not in ("done", "cancelled"):
            return "planning"
        if not builds:
            return "failed" if plan_status == "cancelled" else "active"
        if all(t["status"] in SATISFIED_DEP_STATUSES for t in builds) and any(t["status"] == "done" for t in builds):
            return "done"
        return "active"

    def _refresh_project(self, project_id: str, *, broadcast: bool = True, force: bool = False) -> None:
        project = self.store.projects.get(project_id)
        if project is None:
            return
        status = self._compute_project_status(project)
        if status != project["status"] or force:
            project["status"] = status
            if broadcast:
                self._broadcast({"type": "project", "project": project})
            self._changed()

    # ------------------------------------------------------------ worker records
    def _worker_status(self, worker: dict) -> str:
        if worker["id"] not in self.conns:
            return "offline"
        return "busy" if worker["running"] else "idle"

    def _refresh_worker(self, worker_id: str, *, force: bool = False) -> None:
        worker = self.store.workers.get(worker_id)
        if worker is None:
            return
        status = self._worker_status(worker)
        if force or status != worker["status"]:
            worker["status"] = status
            self._broadcast({"type": "worker", "worker": worker})
            self._changed()

    def _send_worker(self, worker_id: str, frame: dict) -> bool:
        conn = self.conns.get(worker_id)
        return bool(conn and conn.peer.send(frame))

    def _release(self, task: dict) -> str | None:
        """Remove a task from its worker's running list. Returns that worker id (if any)."""
        for worker in self.store.workers.values():
            if task["id"] in worker["running"]:
                worker["running"].remove(task["id"])
                self._refresh_worker(worker["id"], force=True)
                return worker["id"]
        return None

    def _stop_on_worker(self, task: dict, reason: str) -> None:
        """If the task is executing somewhere, tell that worker to kill it and free the slot."""
        wid = self._release(task)
        if wid:
            self._send_worker(wid, {"type": "cancel_task", "task_id": task["id"], "reason": reason})

    # ------------------------------------------------------------ task transitions
    def _deps_satisfied(self, task: dict) -> bool:
        for dep in task["depends_on"]:
            d = self.store.tasks.get(dep)
            if d is not None and d["status"] not in SATISFIED_DEP_STATUSES:
                return False
        return True

    def _initial_status(self, task: dict) -> str:
        return "todo" if self._deps_satisfied(task) else "backlog"

    def _promote_backlog(self) -> None:
        t = now()
        for task in self.store.tasks.values():
            if task["status"] == "backlog" and self._deps_satisfied(task):
                task["status"] = "todo"
                self._event(task, "system", "dependencies satisfied; moved backlog -> todo", ts=t)
                self._touch(task, t)

    def _claim(self, task: dict, conn: WorkerConn, agent: str, t: float) -> None:
        worker = self.store.workers[conn.worker_id]
        meta = self.store.meta_of(task["id"])
        token = new_token()
        self.store.set_token(task["id"], token)
        meta.update(agent=agent, claimed_at=t, tokens_base=dict(task["tokens"]))
        meta.pop("hold_until", None)
        meta.pop("plan_error", None)
        task.update(
            status="claimed",
            assignee={"worker_id": worker["id"], "laptop": worker["laptop"], "owner": worker["owner"], "agent": agent},
            attempts=task["attempts"] + 1,
            started_at=None, finished_at=None, error="", progress=0, progress_note="",
        )
        worker["running"].append(task["id"])
        project = self.store.projects[task["project_id"]]
        conn.peer.send({
            "type": "run_task",
            "task": task,
            "project": project,
            "board": [board_entry(x) for x in self.store.project_tasks(project["id"])],
            "agent": agent,
            "server_http": conn.http_base,
            "token": token,
        })
        self._event(task, "system", f"claimed by {worker['id']} ({agent}), attempt {task['attempts']}", ts=t)
        self._touch(task, t)
        self._refresh_worker(worker["id"], force=True)
        self._refresh_project(task["project_id"])

    def _requeue(self, task: dict, reason: str, t: float, *, hold: float = 0.0) -> None:
        """Back to todo (or failed once max_attempts is reached), e.g. after a worker vanished."""
        self._release(task)
        if task["attempts"] >= self.s.max_attempts:
            task.update(status="failed", error=reason, finished_at=t)
            self._event(task, "error", f"{reason}; giving up after {task['attempts']} attempts", ts=t)
        else:
            task.update(status="todo", assignee=None, progress=0, progress_note="", started_at=None)
            if hold > 0:
                self.store.meta_of(task["id"])["hold_until"] = t + hold
            self._event(task, "system", f"{reason}; back to todo", ts=t)
        self._touch(task, t)
        self._refresh_project(task["project_id"])

    def _retry_or_fail(self, task: dict, error: str, t: float) -> None:
        """A failed attempt (task_finished ok=false, bad plan, ...)."""
        error = truncate(error or "attempt failed", 2000)
        self._release(task)
        if task["attempts"] < self.s.max_attempts:
            task.update(status="todo", assignee=None, error=error, progress=0, progress_note="",
                        started_at=None, finished_at=None)
            self._event(task, "error", f"attempt {task['attempts']} failed: {one_line(error, 500)}; will retry", ts=t)
        else:
            task.update(status="failed", error=error, finished_at=t)
            self._event(task, "error", f"failed after {task['attempts']} attempts: {one_line(error, 500)}", ts=t)
        self._touch(task, t)
        self._refresh_project(task["project_id"])

    def _mark_done(self, task: dict, t: float, note: str) -> None:
        task.update(status="done", progress=100, finished_at=t, error="")
        self._event(task, "system", note, ts=t)
        self._touch(task, t)
        self._promote_backlog()
        self._refresh_project(task["project_id"])
        if task["kind"] == "build":
            pr = f" ({task['pr_url']})" if task["pr_url"] else ""
            self._post_message(task["project_id"], {"kind": "system", "name": "hub"}, "all",
                               f"{task['key']} · {task['title']} is done{pr}.")

    # ================================================================ scheduler
    def tick(self, t: float) -> None:
        """One scheduler pass (lock held). `t` is injectable for tests."""
        # 1. offline detection: silent workers are dropped (socket closed -> they re-hello)
        for wid, conn in list(self.conns.items()):
            worker = self.store.workers[wid]
            if t - worker["last_seen"] > self.s.offline_after:
                self._drop_worker(conn, f"worker {wid} went offline (no message for {self.s.offline_after:.0f}s)", t)
                conn.peer.close(4000)
        # 2. claim timeouts
        for wid in list(self.conns):
            for tid in list(self.store.workers[wid]["running"]):
                task = self.store.tasks.get(tid)
                claimed_at = self.store.meta_of(tid).get("claimed_at", t)
                if task and task["status"] == "claimed" and t - claimed_at > self.s.claim_timeout:
                    self._send_worker(wid, {"type": "cancel_task", "task_id": tid,
                                            "reason": "not started within the claim timeout"})
                    # short hold so a late reply about the cancelled attempt cannot hit the next one
                    self._requeue(task, f"{wid} did not start it within {self.s.claim_timeout:.0f}s", t,
                                  hold=CANCEL_SETTLE)
        # 3. dependencies
        self._promote_backlog()
        # 4. assignment
        slots = []
        for wid, conn in self.conns.items():
            w = self.store.workers[wid]
            slots.append(WorkerSlot(wid, list(w["agents"]), w["max_concurrent"], len(w["running"]), conn.connected_at))
        hold = {tid: m["hold_until"] for tid, m in self.store.meta.items() if m.get("hold_until")}
        for a in schedule(self.store.tasks, slots, t, hold):
            self._claim(self.store.tasks[a.task_id], self.conns[a.worker_id], a.agent, t)

    # ================================================================ WS /worker
    def worker_hello(self, peer: Peer, msg: dict, http_base: str, t: float) -> WorkerConn:
        info = msg.get("worker") if isinstance(msg.get("worker"), dict) else {}
        wid = str(info.get("id") or "").strip()
        if not wid:
            raise ApiError(400, "bad_hello", "hello.worker.id is required")
        old = self.conns.get(wid)
        if old is not None and old.peer is not peer:
            print(f"[hub] worker {wid} reconnected; closing its previous socket")
            old.peer.close(4001)
        worker = self.store.workers.setdefault(wid, {"id": wid, "running": [], "status": "offline"})
        worker.update(
            id=wid,
            laptop=str(info.get("laptop") or wid),
            owner=str(info.get("owner") or ""),
            tailscale_ip=str(info.get("tailscale_ip") or ""),
            agents=str_list(info.get("agents")),
            max_concurrent=clamp_int(info.get("max_concurrent"), 1, 64, 1),
            version=str(info.get("version") or ""),
            connected_at=t,
            last_seen=t,
        )
        worker.setdefault("running", [])
        conn = WorkerConn(wid, peer, http_base, t)
        self.conns[wid] = conn
        peer.send({"type": "welcome", "worker_id": wid, "server_version": SERVER_VERSION, "server_time": t})
        print(f"[hub] worker {wid} hello: agents={worker['agents']} max={worker['max_concurrent']} http={http_base}")

        reported = str_list(msg.get("running"))
        # tasks we thought ran here but the worker no longer has (e.g. run_task lost in a reconnect)
        for tid in list(worker["running"]):
            if tid not in reported:
                task = self.store.tasks.get(tid)
                worker["running"].remove(tid)
                if task and task["status"] in ACTIVE_STATUSES:
                    self._requeue(task, f"{wid} reconnected without it", t)
        self._reconcile_reported(wid, reported, t, adopt=True)
        self._refresh_worker(wid, force=True)
        self._changed()
        return conn

    def _reconcile_reported(self, wid: str, reported: list[str], t: float, *, adopt: bool) -> None:
        """
        Compare a worker's own `running` list with ours.
        adopt=True (hello): re-attach tasks it still runs after a reconnect / hub restart if they
        are unassigned (todo) or already ours. Anything else it runs gets a cancel_task.
        """
        worker = self.store.workers[wid]
        for tid in reported:
            task = self.store.tasks.get(tid)
            if task is None:
                self._send_worker(wid, {"type": "cancel_task", "task_id": tid, "reason": "unknown task"})
                continue
            mine = (task["assignee"] or {}).get("worker_id") == wid
            if mine and tid in worker["running"]:
                continue
            reattach = task["status"] == "todo" or (mine and task["status"] in ACTIVE_STATUSES)
            if adopt and reattach:
                meta = self.store.meta_of(tid)
                meta.pop("hold_until", None)
                meta.setdefault("claimed_at", t)
                if not meta.get("token"):
                    self.store.set_token(tid, new_token())
                agent = (task["assignee"] or {}).get("agent") or meta.get("agent") or (worker["agents"] or ["?"])[0]
                task["assignee"] = {"worker_id": wid, "laptop": worker["laptop"], "owner": worker["owner"], "agent": agent}
                if task["status"] != "review":
                    task["status"] = "in_progress"
                    task["started_at"] = task["started_at"] or t
                worker["running"].append(tid)
                self._event(task, "system", f"re-attached to {wid} after reconnect", ts=t)
                self._touch(task, t)
                self._refresh_project(task["project_id"])
            elif not (mine and task["status"] in ACTIVE_STATUSES):
                why = f"task is {task['status']}" if mine or not task["assignee"] else \
                    f"task is now assigned to {task['assignee']['worker_id']}"
                self._send_worker(wid, {"type": "cancel_task", "task_id": tid, "reason": why})

    def worker_disconnected(self, conn: WorkerConn, t: float) -> None:
        if self.conns.get(conn.worker_id) is conn:
            self._drop_worker(conn, f"worker {conn.worker_id} disconnected", t)

    def _drop_worker(self, conn: WorkerConn, reason: str, t: float) -> None:
        wid = conn.worker_id
        self.conns.pop(wid, None)
        worker = self.store.workers[wid]
        for tid in list(worker["running"]):
            task = self.store.tasks.get(tid)
            if task is None or task["status"] not in ACTIVE_STATUSES:
                worker["running"].remove(tid)
                continue
            if task["status"] == "review":
                # git push / PR in flight; keep it parked on this worker so a re-hello can re-attach
                worker["running"].remove(tid)
                self._event(task, "system", f"{reason} during review; waiting for it to re-attach", ts=t)
                continue
            self._requeue(task, reason, t, hold=self.s.reattach_grace)
        worker["running"] = []
        print(f"[hub] {reason}")
        self._refresh_worker(wid, force=True)
        self._changed()

    def worker_message(self, conn: WorkerConn, msg: dict, t: float) -> None:
        """Dispatch one worker -> server frame (anything after hello)."""
        if self.conns.get(conn.worker_id) is not conn:
            return  # superseded socket
        worker = self.store.workers[conn.worker_id]
        worker["last_seen"] = t
        kind = msg.get("type")
        handler = {
            "heartbeat": self._on_heartbeat,
            "task_started": self._on_task_started,
            "task_event": self._on_task_event,
            "task_progress": self._on_task_progress,
            "task_usage": self._on_task_usage,
            "task_status": self._on_task_status,
            "task_finished": self._on_task_finished,
            "plan_result": self._on_plan_result,
        }.get(kind)
        if kind == "hello":
            return  # duplicate hello on the same socket: ignore
        if handler is None:
            self.warn_once(f"type:{kind}", f"ignoring unknown worker message type {kind!r}")
            return
        handler(conn, msg, t)

    def _own_task(self, conn: WorkerConn, msg: dict) -> dict | None:
        """The task named by msg.task_id if it is (still) assigned to this worker."""
        task = self.store.tasks.get(str(msg.get("task_id") or ""))
        stale_attempt = task is not None and msg.get("attempt") is not None and \
            clamp_int(msg.get("attempt"), 0, 10**6, -1) != task["attempts"]
        if task is None or stale_attempt or (task["assignee"] or {}).get("worker_id") != conn.worker_id:
            self.warn_once(f"stale:{conn.worker_id}:{msg.get('task_id')}:{msg.get('type')}",
                            f"ignoring {msg.get('type')} from {conn.worker_id} for task "
                            f"{msg.get('task_id')!r} not assigned to it")
            return None
        return task

    def _on_heartbeat(self, conn: WorkerConn, msg: dict, t: float) -> None:
        reported = str_list(msg.get("running"))
        worker = self.store.workers[conn.worker_id]
        # a finished-but-not-reported task (e.g. plan accepted via `ov plan`, task_finished lost)
        for tid in list(worker["running"]):
            task = self.store.tasks.get(tid)
            if tid not in reported and (task is None or task["status"] not in ACTIVE_STATUSES):
                worker["running"].remove(tid)
                self._refresh_worker(conn.worker_id, force=True)
        self._reconcile_reported(conn.worker_id, reported, t, adopt=False)
        self._refresh_worker(conn.worker_id)

    def _on_task_started(self, conn: WorkerConn, msg: dict, t: float) -> None:
        task = self._own_task(conn, msg)
        if task is None:
            return
        if task["status"] not in ("claimed", "in_progress"):
            return
        agent = str(msg.get("agent") or task["assignee"].get("agent") or "")
        task["assignee"] = {**task["assignee"], "agent": agent}
        self.store.meta_of(task["id"])["agent"] = agent
        task.update(status="in_progress", started_at=t)
        if msg.get("branch"):
            task["branch"] = str(msg["branch"])
        where = f" in {msg['workspace']}" if msg.get("workspace") else ""
        self._event(task, "system", f"started on {conn.worker_id} with {agent}{where}", ts=t)
        self._touch(task, t)

    def _on_task_event(self, conn: WorkerConn, msg: dict, t: float) -> None:
        task = self._own_task(conn, msg)
        if task is None:
            return
        events = msg.get("events") if isinstance(msg.get("events"), list) else [msg.get("event")]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            kind = str(ev.get("kind") or "system")
            if kind not in EVENT_KINDS:
                self.warn_once(f"evkind:{kind}", f"unknown event kind {kind!r} (stored as-is)")
            data = ev.get("data") if isinstance(ev.get("data"), dict) else None
            if data and len(json.dumps(data, ensure_ascii=False, default=str)) > MAX_EVENT_DATA:
                data = {"truncated": True}
            ts = ev.get("ts") if isinstance(ev.get("ts"), (int, float)) else t
            self._event(task, kind, ev.get("text") or "", tool=str(ev.get("tool") or ""), data=data, ts=ts)
        task["updated_at"] = t
        self._pending_task_frames.add(task["id"])

    def _on_task_progress(self, conn: WorkerConn, msg: dict, t: float) -> None:
        task = self._own_task(conn, msg)
        if task is None:
            return
        self._set_progress(task, msg.get("progress"), msg.get("note"), t)

    def _set_progress(self, task: dict, progress: object, note: object, t: float) -> None:
        task["progress"] = clamp_int(progress, 0, 100, task["progress"])
        if note is not None:
            task["progress_note"] = one_line(note, 300)
        self._touch(task, t)

    def _on_task_usage(self, conn: WorkerConn, msg: dict, t: float) -> None:
        task = self._own_task(conn, msg)
        if task is None or not isinstance(msg.get("tokens"), dict):
            return
        base = self.store.meta_of(task["id"]).get("tokens_base") or {"input": 0, "output": 0}
        task["tokens"] = {k: base.get(k, 0) + clamp_int(msg["tokens"].get(k), 0, 10**12, 0) for k in ("input", "output")}
        task["updated_at"] = t
        self._pending_task_frames.add(task["id"])
        self._changed(kick=False)

    def _on_task_status(self, conn: WorkerConn, msg: dict, t: float) -> None:
        task = self._own_task(conn, msg)
        if task is None:
            return
        if msg.get("status") != "review":
            self.warn_once(f"task_status:{msg.get('status')}", f"ignoring task_status {msg.get('status')!r}")
            return
        if task["status"] not in ("claimed", "in_progress"):
            return
        task["status"] = "review"
        note = one_line(msg.get("note") or "pushing branch / opening PR", 300)
        task["progress_note"] = note
        self._event(task, "git", note, ts=t)
        self._touch(task, t)

    def _on_task_finished(self, conn: WorkerConn, msg: dict, t: float) -> None:
        task = self._own_task(conn, msg)
        if task is None:
            return
        ok = bool(msg.get("ok"))
        for field in ("branch", "pr_url"):
            if msg.get(field):
                task[field] = str(msg[field])
        if msg.get("result"):
            task["result"] = truncate(msg["result"], 20000)
        if task["status"] not in ACTIVE_STATUSES:
            # already resolved (plan accepted via `ov plan`, cancelled / done by a human): just free the slot
            self._release(task)
            self._event(task, "system", f"worker reported finish (ok={ok}) after task was {task['status']}", ts=t)
            self._touch(task, t)
            return
        if task["kind"] == "plan":
            plan_error = self.store.meta_of(task["id"]).pop("plan_error", "")
            error = msg.get("error") or plan_error or (
                "planner finished without submitting a plan" if ok else "planner failed")
            self._retry_or_fail(task, str(error), t)
            return
        if not ok:
            self._retry_or_fail(task, str(msg.get("error") or "agent failed"), t)
            return
        project = self.store.projects[task["project_id"]]
        merged = bool(msg.get("merged"))
        self._release(task)
        if merged or not project["repo"]:
            how = "merged" if merged else "finished (local mode)"
            self._mark_done(task, t, f"{how}{': ' + task['pr_url'] if task['pr_url'] else ''}")
        else:
            task.update(status="review", progress=100, progress_note="PR open, waiting for a human to merge")
            self._event(task, "git", f"PR left open for review: {task['pr_url'] or '(no url)'}", ts=t)
            self._touch(task, t)
            self._refresh_project(task["project_id"])

    def _on_plan_result(self, conn: WorkerConn, msg: dict, t: float) -> None:
        task = self._own_task(conn, msg)
        if task is None:
            return
        if task["kind"] != "plan":
            self.warn_once(f"plan_result:{task['id']}", f"plan_result for non-plan task {task['id']}")
            return
        if task["status"] == "done":
            return  # already planned (e.g. the agent used `ov plan`)
        try:
            self._apply_plan(task, msg.get("title"), msg.get("summary"), msg.get("tasks"), t)
        except PlanError as exc:
            # keep the attempt alive: its task_finished (which follows) triggers the retry, so a
            # late finish of this attempt can never be mistaken for the next one
            self.store.meta_of(task["id"])["plan_error"] = f"invalid plan: {exc}"
            self._event(task, "error", f"plan rejected: {exc}", ts=t)
            self._touch(task, t)

    # ================================================================ planning
    def _next_task_number(self, project_id: str) -> int:
        return max((task_number(t["id"]) for t in self.store.tasks.values() if t["project_id"] == project_id),
                   default=0) + 1

    def _apply_plan(self, plan_task: dict, title: object, summary: object, raw_items: object, t: float) -> list[dict]:
        """Validate + ingest a plan (raises PlanError). Completes the PLAN task."""
        items = normalize_plan(raw_items)
        project = self.store.projects[plan_task["project_id"]]
        planned, dropped = map_plan(project["id"], items, self._next_task_number(project["id"]), self.store.tasks)
        created = []
        for p in planned:
            task = new_task(
                task_id=p.task_id, key=p.key, project_id=project["id"], kind="build",
                title=p.item["title"], description=p.item["description"], acceptance=p.item["acceptance"],
                depends_on=p.depends_on, priority=p.item["priority"], agent_hint=p.item["agent_hint"], ts=t,
            )
            self.store.tasks[task["id"]] = task
            created.append(task)
        for task in created:  # statuses after all exist, so intra-plan deps resolve
            task["status"] = self._initial_status(task)
            self._event(task, "system", f"created by the planner ({task['status']})", ts=t)
            self._touch(task, t)
        if title and str(title).strip():
            project["title"] = truncate(" ".join(str(title).split()), 120)
        project["summary"] = truncate(str(summary or ""), 4000)
        note = f"plan accepted: {len(created)} tasks"
        if dropped:
            note += "; dropped dependencies: " + ", ".join(dropped)
        plan_task.update(result=project["summary"] or note, progress=100, progress_note=note)
        self._mark_done(plan_task, t, note)
        self._refresh_project(project["id"], force=True)
        self._post_message(project["id"], {"kind": "system", "name": "hub"}, "all",
                           f"Plan ready for {project['title']}: " + ", ".join(f"{x['key']} {x['title']}" for x in created))
        return created

    # ================================================================ messages
    def _post_message(self, project_id: str, sender: dict, to: str, text: str, reply_to: str | None = None) -> dict:
        self.store.msg_counter += 1
        msg = {
            "id": message_id(self.store.msg_counter),
            "project_id": project_id,
            "ts": now(),
            "from": sender,
            "to": to or "all",
            "text": truncate(text, MAX_MESSAGE_TEXT),
            "reply_to": reply_to or None,
        }
        self.store.append_message(msg)
        self._broadcast({"type": "message", "message": msg})
        self._forward_chat(msg)
        # wake every inbox long-poll
        self._msg_event.set()
        self._msg_event = asyncio.Event()
        self._changed()
        return msg

    def _forward_chat(self, msg: dict) -> None:
        for wid in self.conns:
            running = set(self.store.workers[wid]["running"])
            projects = {self.store.tasks[tid]["project_id"] for tid in running if tid in self.store.tasks}
            to = msg["to"]
            if to in running or to == wid or (to == "all" and msg["project_id"] in projects):
                self._send_worker(wid, {"type": "chat", "message": msg})

    # ================================================================ snapshots
    def state(self) -> dict:
        return {
            "projects": sorted(self.store.projects.values(), key=lambda p: p["created_at"]),
            "tasks": self.store.all_tasks(),
            "workers": sorted(self.store.workers.values(), key=lambda w: w["id"]),
            "messages": self.store.messages[-MAX_STATE_MESSAGES:],
        }

    def add_board_peer(self, peer: Peer) -> None:
        peer.send({"type": "snapshot", **self.state()})
        self.board_peers.add(peer)

    def legacy_laptops(self, t: float) -> list[dict]:
        """Reporter laptops + one synthesized entry per connected worker (merged by tailscale_ip)."""
        self.legacy.prune(t)
        laptops = self.legacy.fresh(t)
        by_ip = {lap.get("tailscale_ip"): lap for lap in laptops if lap.get("tailscale_ip")}
        for wid in sorted(self.conns):
            worker = self.store.workers[wid]
            agents = []
            for tid in worker["running"]:
                task = self.store.tasks.get(tid)
                if task is None:
                    continue
                agents.append({
                    "name": (task["assignee"] or {}).get("agent") or "",
                    "kind": "task", "task_id": tid,  # lets the AR view stop/retry/message it
                    "pid": 0, "cpu": 0, "cwd": "",
                    "last_log": task["last_event"],
                    "summary": f"{task['key']} · {task['title']} — {task['progress']}%",
                    # synthesized orchestra worker agent; brain.annotate keeps its summary as-is
                    "agent_id": f"task:{tid}",
                })
            match = by_ip.get(worker["tailscale_ip"]) if worker["tailscale_ip"] else None
            if match is not None:
                match["agents"] = list(match.get("agents", [])) + agents
            else:
                entry = {"laptop": worker["laptop"], "owner": worker["owner"],
                         "tailscale_ip": worker["tailscale_ip"], "ts": worker["last_seen"], "agents": agents}
                laptops.append(entry)
                if worker["tailscale_ip"]:
                    # a second worker process on the same laptop joins this entry instead of
                    # being drawn on top of it in the 3D view
                    by_ip[worker["tailscale_ip"]] = entry
        return laptops

    def legacy_view(self, t: float) -> dict:
        """The full WS /view frame: reporter + synthesized laptops, brain-annotated with
        finished summaries and agent states, plus collision alerts and the team headline."""
        laptops = self.legacy_laptops(t)
        self.brain.annotate(laptops, t)
        return {
            "type": "state",
            "ts": t,
            "team_summary": self.brain.team_summary or count_line(laptops),
            "alerts": self.brain.collisions(laptops, t),
            "laptops": laptops,
        }

    # ================================================================ human API
    def human_create_project(self, body: dict) -> dict:
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise ApiError(400, "bad_request", "prompt is required")
        repo = normalize_repo(body.get("repo"))
        base_branch = str(body.get("base_branch") or "main").strip() or "main"
        t = now()
        pid = f"p-{secrets.token_hex(2)}"
        while pid in self.store.projects:
            pid = f"p-{secrets.token_hex(2)}"
        project = {
            "id": pid, "prompt": prompt, "title": default_title(prompt), "repo": repo,
            "base_branch": base_branch, "status": "planning", "created_at": t,
            "plan_task_id": f"{pid}-PLAN", "summary": "",
        }
        self.store.projects[pid] = project
        plan = new_task(task_id=project["plan_task_id"], key="PLAN", project_id=pid, kind="plan",
                        title=f"Plan: {project['title']}", description=prompt, priority=0, status="todo", ts=t)
        self.store.tasks[plan["id"]] = plan
        self._broadcast({"type": "project", "project": project})
        self._event(plan, "system", f"project created ({'repo ' + repo if repo else 'local mode'}); waiting for a planner", ts=t)
        self._touch(plan, t)
        return project

    def human_add_task(self, project_id: str, body: dict) -> dict:
        project = self._project(project_id)
        title = " ".join(str(body.get("title") or "").split())
        if not title:
            raise ApiError(400, "bad_request", "title is required")
        deps = []
        by_key = {x["key"]: x["id"] for x in self.store.project_tasks(project_id)}
        for dep in str_list(body.get("depends_on")):
            tid = dep if self.store.tasks.get(dep, {}).get("project_id") == project_id else by_key.get(dep)
            if tid is None:
                raise ApiError(400, "unknown_dependency", f"no task {dep!r} in project {project_id}")
            if tid not in deps:
                deps.append(tid)
        n = self._next_task_number(project_id)
        t = now()
        task = new_task(
            task_id=f"{project_id}-T{n}", key=f"T{n}", project_id=project_id, kind="build",
            title=truncate(title, 200), description=str(body.get("description") or ""),
            acceptance=str_list(body.get("acceptance")), depends_on=deps,
            priority=clamp_int(body.get("priority"), 1, 4, 2), agent_hint=str(body.get("agent_hint") or "").strip(), ts=t,
        )
        self.store.tasks[task["id"]] = task
        task["status"] = self._initial_status(task)
        name = str(body.get("name") or "").strip()
        self._event(task, "system", f"added by {name or 'a human'} ({task['status']})", ts=t)
        self._touch(task, t)
        self._refresh_project(project["id"])
        return task

    def human_retry(self, task_id: str) -> dict:
        task = self._task(task_id)
        if task["kind"] == "plan" and task["status"] == "done" and any(
                x["kind"] == "build" for x in self.store.project_tasks(task["project_id"])):
            raise ApiError(409, "already_planned", "the plan was already applied; add tasks instead")
        t = now()
        self._stop_on_worker(task, "retry requested by a human")
        self.store.revoke_token(task_id)
        self.store.meta_of(task_id).pop("hold_until", None)
        task.update(attempts=0, error="", assignee=None, progress=0, progress_note="", result="", pr_url="",
                    started_at=None, finished_at=None)
        task["status"] = self._initial_status(task) if task["kind"] == "build" else "todo"
        self._event(task, "system", f"retry requested by a human ({task['status']})", ts=t)
        self._touch(task, t)
        self._refresh_project(task["project_id"])
        return task

    def human_cancel(self, task_id: str) -> dict:
        task = self._task(task_id)
        if task["status"] == "cancelled":
            return task
        if task["status"] == "done":
            raise ApiError(409, "already_done", f"{task['key']} is already done")
        t = now()
        self._stop_on_worker(task, "cancelled by a human")
        self.store.revoke_token(task_id)
        task.update(status="cancelled", finished_at=t)
        self._event(task, "system", "cancelled by a human", ts=t)
        self._touch(task, t)
        self._promote_backlog()
        self._refresh_project(task["project_id"])
        return task

    def human_done(self, task_id: str) -> dict:
        task = self._task(task_id)
        if task["status"] == "done":
            return task
        self._stop_on_worker(task, "marked done by a human")
        self._mark_done(task, now(), "marked done by a human")
        return task

    def human_message(self, body: dict) -> dict:
        project = self._project(str(body.get("project_id") or ""))
        text = str(body.get("text") or "").strip()
        if not text:
            raise ApiError(400, "bad_request", "text is required")
        name = str(body.get("name") or "").strip() or "human"
        return self._post_message(project["id"], {"kind": "human", "name": name},
                                  str(body.get("to") or "all"), text, body.get("reply_to"))

    def project_view(self, project_id: str) -> dict:
        project = self._project(project_id)
        return {"project": project, "tasks": self.store.project_tasks(project_id)}

    def task_view(self, task_id: str) -> dict:
        task = self._task(task_id)
        return {"task": task, "events": self.store.events.get(task_id, [])}

    # ================================================================ agent API
    def agent_auth(self, authorization: str | None) -> dict:
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        tid = self.store.token_index.get(token) if token else None
        if tid is None or tid not in self.store.tasks:
            raise ApiError(401, "unauthorized", "missing or invalid bearer token (use $OV_TOKEN)")
        return self.store.tasks[tid]

    def _agent_identity(self, task: dict) -> dict:
        assignee = task["assignee"] or {}
        agent = assignee.get("agent") or self.store.meta_of(task["id"]).get("agent") or "agent"
        wid = assignee.get("worker_id") or ""
        return {"kind": "agent", "task_id": task["id"], "worker_id": wid, "agent": agent,
                "name": f"{task['key']} · {agent}@{wid}" if wid else f"{task['key']} · {agent}"}

    def agent_say(self, task: dict, body: dict) -> dict:
        text = str(body.get("text") or "").strip()
        if not text:
            raise ApiError(400, "bad_request", "text is required")
        return self._post_message(task["project_id"], self._agent_identity(task),
                                  str(body.get("to") or "all"), text, body.get("reply_to"))

    def agent_inbox(self, task: dict, since: str) -> tuple[list[dict], str, asyncio.Event]:
        """Messages for this task after `since`, the next cursor, and an Event set on the next new message."""
        after = message_num(since)
        wid = (task["assignee"] or {}).get("worker_id") or ""
        out = []
        for m in self.store.messages:
            if message_num(m["id"]) <= after or m["project_id"] != task["project_id"]:
                continue
            if m["to"] not in ("all", task["id"], wid) or (m["from"].get("task_id") == task["id"]):
                continue
            out.append(m)
            if len(out) >= MAX_INBOX_BATCH:
                break
        if len(out) >= MAX_INBOX_BATCH:
            cursor = out[-1]["id"]
        elif self.store.msg_counter > after:
            cursor = message_id(self.store.msg_counter)
        else:
            cursor = since or ""
        return out, cursor, self._msg_event

    def agent_progress(self, task: dict, body: dict) -> dict:
        self._set_progress(task, body.get("progress"), body.get("note", ""), now())
        return task

    def agent_board(self, task: dict) -> dict:
        project = self.store.projects[task["project_id"]]
        return {"project": project, "tasks": [agent_board_entry(x) for x in self.store.project_tasks(project["id"])]}

    def agent_plan(self, task: dict, body: dict) -> dict:
        if task["kind"] != "plan":
            raise ApiError(403, "not_planner", "only the PLAN task may submit a plan")
        if task["status"] == "done":
            raise ApiError(409, "already_planned", "this project already has a plan")
        if task["status"] in ("failed", "cancelled"):
            raise ApiError(409, "plan_closed", f"the plan task is {task['status']}")
        try:
            created = self._apply_plan(task, body.get("title"), body.get("summary"), body.get("tasks"), now())
        except PlanError as exc:
            raise ApiError(400, "invalid_plan", str(exc)) from None
        return {"project": self.store.projects[task["project_id"]], "tasks": created}
