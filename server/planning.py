"""
Plan validation and ref -> task-id mapping (PROTOCOL.md §5).

    items = normalize_plan(raw_items)              # raises PlanError on invalid input
    built = map_plan(project_id, items, first_n, existing_tasks)

`map_plan` turns plan-local refs (T1..Tn) into `<project>-T<n>` ids in order and drops
unknown / self / duplicate / cyclic dependencies (cycle edges are dropped in plan order:
an edge is kept unless it would close a cycle with the edges kept before it).
Pure functions, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from models import clamp_int, str_list, truncate

MIN_ITEMS = 1
MAX_ITEMS = 20


class PlanError(ValueError):
    """The submitted plan is unusable (message is shown to the planner / on the board)."""


@dataclass
class PlannedTask:
    task_id: str
    key: str
    item: dict
    depends_on: list[str] = field(default_factory=list)


def normalize_plan(raw_items: object) -> list[dict]:
    """Validate PlanItems (1-20 items, non-empty titles) and coerce field types."""
    if not isinstance(raw_items, list):
        raise PlanError("plan 'tasks' must be a list of PlanItem objects")
    if not MIN_ITEMS <= len(raw_items) <= MAX_ITEMS:
        raise PlanError(f"plan must have {MIN_ITEMS}-{MAX_ITEMS} tasks, got {len(raw_items)}")
    items: list[dict] = []
    for i, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise PlanError(f"plan item {i} is not an object")
        title = " ".join(str(raw.get("title") or "").split())
        if not title:
            raise PlanError(f"plan item {i} has an empty title")
        items.append({
            "ref": str(raw.get("ref") or f"T{i}").strip() or f"T{i}",
            "title": truncate(title, 200),
            "description": str(raw.get("description") or ""),
            "acceptance": str_list(raw.get("acceptance")),
            "depends_on": str_list(raw.get("depends_on")),
            "priority": clamp_int(raw.get("priority"), 1, 4, 2),
            "agent_hint": str(raw.get("agent_hint") or "").strip(),
        })
    return items


def _reaches(graph: dict[str, list[str]], start: str, target: str) -> bool:
    """True if `target` is reachable from `start` following dependency edges."""
    stack, seen = [start], set()
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, ()))
    return False


def map_plan(
    project_id: str,
    items: list[dict],
    first_n: int,
    existing: dict[str, dict],
) -> tuple[list[PlannedTask], list[str]]:
    """
    Assign ids `<project>-T<first_n + i>` and resolve dependencies.

    A dependency may name a plan ref (preferred), or an id / key of a task that already
    exists in the project (e.g. one a human added while planning ran).
    Returns (planned tasks, human-readable notes about dropped dependencies).
    """
    planned = [
        PlannedTask(task_id=f"{project_id}-T{first_n + i}", key=f"T{first_n + i}", item=item)
        for i, item in enumerate(items)
    ]
    ref_map: dict[str, str] = {}
    for p in planned:
        ref_map.setdefault(p.item["ref"], p.task_id)
        ref_map.setdefault(p.item["ref"].upper(), p.task_id)
    existing_by_key = {t["key"]: tid for tid, t in existing.items() if t["project_id"] == project_id}

    dropped: list[str] = []
    graph: dict[str, list[str]] = {p.task_id: [] for p in planned}
    for p in planned:
        ref = p.item["ref"]
        for dep in p.item["depends_on"]:
            target = ref_map.get(dep) or ref_map.get(dep.upper())
            if target is None and dep in existing and existing[dep]["project_id"] == project_id:
                target = dep
            if target is None:
                target = existing_by_key.get(dep)
            if target is None:
                dropped.append(f"{ref} -> {dep} (unknown)")
                continue
            if target == p.task_id:
                dropped.append(f"{ref} -> {dep} (self)")
                continue
            if target in p.depends_on:
                continue
            if target in graph and _reaches(graph, target, p.task_id):
                dropped.append(f"{ref} -> {dep} (cycle)")
                continue
            p.depends_on.append(target)
            graph[p.task_id].append(target)
    return planned, dropped
