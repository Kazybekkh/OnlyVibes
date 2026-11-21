"""
Task -> worker assignment policy (PROTOCOL.md §2). Pure function, no I/O.

    assignments = schedule(tasks_by_id, slots, now, hold_until)

Rules:
  * only `todo` tasks whose re-attach hold (see hub.REATTACH) has expired are candidates;
  * plan tasks first, then build tasks by priority asc, locality (dependencies this worker
    executed itself) when priorities tie, created_at asc, key in natural order;
  * a task with an `agent_hint` is skipped by workers lacking that adapter unless no
    connected worker has it;
  * free slots are filled round-robin (least-loaded worker first) so one laptop does not
    swallow the whole board while others idle.
Agent choice: `agent_hint` if this worker has it, else the worker's first (preferred) agent.
"""
from __future__ import annotations

from dataclasses import dataclass

from models import natural_key


@dataclass
class WorkerSlot:
    worker_id: str
    agents: list[str]
    max_concurrent: int
    running: int
    connected_at: float = 0.0

    @property
    def free(self) -> int:
        return self.max_concurrent - self.running


@dataclass(frozen=True)
class Assignment:
    task_id: str
    worker_id: str
    agent: str


def choose_agent(task: dict, agents: list[str]) -> str:
    hint = task.get("agent_hint") or ""
    return hint if hint in agents else agents[0]


def _locality(task: dict, worker_id: str, tasks_by_id: dict[str, dict]) -> int:
    count = 0
    for dep in task.get("depends_on", ()):
        assignee = (tasks_by_id.get(dep) or {}).get("assignee") or {}
        if assignee.get("worker_id") == worker_id:
            count += 1
    return count


def _order(task: dict, worker_id: str, tasks_by_id: dict[str, dict]) -> tuple:
    return (
        0 if task["kind"] == "plan" else 1,
        task.get("priority", 2),
        -_locality(task, worker_id, tasks_by_id),
        task.get("created_at", 0.0),
        natural_key(task.get("key", "")),
        task["id"],
    )


def schedule(
    tasks_by_id: dict[str, dict],
    slots: list[WorkerSlot],
    now: float,
    hold_until: dict[str, float] | None = None,
) -> list[Assignment]:
    hold_until = hold_until or {}
    ready = [
        t for t in tasks_by_id.values()
        if t["status"] == "todo" and hold_until.get(t["id"], 0.0) <= now
    ]
    slots = [s for s in slots if s.agents]
    if not ready or not slots:
        return []
    online_agents = {a for s in slots for a in s.agents}

    def runnable(task: dict, slot: WorkerSlot) -> bool:
        hint = task.get("agent_hint") or ""
        return not hint or hint in slot.agents or hint not in online_agents

    out: list[Assignment] = []
    progressed = True
    while ready and progressed:
        progressed = False
        for slot in sorted(slots, key=lambda s: (s.running, s.connected_at, s.worker_id)):
            if slot.free <= 0:
                continue
            eligible = [t for t in ready if runnable(t, slot)]
            if not eligible:
                continue
            best = min(eligible, key=lambda t: _order(t, slot.worker_id, tasks_by_id))
            ready.remove(best)
            slot.running += 1
            out.append(Assignment(best["id"], slot.worker_id, choose_agent(best, slot.agents)))
            progressed = True
            if not ready:
                break
    return out
