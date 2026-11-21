"""
Plan extraction + validation for PLAN tasks (PROTOCOL.md §5).

A planner agent submits its plan either with `ov plan plan.json` (the CLI keeps
a copy in $OV_STATE_DIR/plan.json) or by ending its final message with a
fenced ```json block. This module finds the plan in either place and turns it
into a clean list of PlanItems:

    {"ref": "T1", "title": "...", "description": "...", "acceptance": ["..."],
     "depends_on": ["T1"], "priority": 2, "agent_hint": ""}

No I/O besides reading the optional plan file; used by worker.py and tests.
"""
from __future__ import annotations

import json
import re

MAX_ITEMS = 20
_FENCE = re.compile(r"```(?:json|JSON)?[ \t]*\r?\n(.*?)\r?\n?```", re.S)


class PlanError(ValueError):
    pass


def _as_plan(obj) -> dict | None:
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
        return {"title": "", "summary": "", "tasks": obj}
    if isinstance(obj, dict):
        if isinstance(obj.get("tasks"), list):
            return obj
        if isinstance(obj.get("plan"), (dict, list)):
            return _as_plan(obj["plan"])
    return None


def extract_plan(text: str) -> dict | None:
    """Return the LAST fenced JSON block in `text` that looks like a plan."""
    if not text:
        return None
    for block in reversed(_FENCE.findall(text)):
        try:
            plan = _as_plan(json.loads(block))
        except ValueError:
            continue
        if plan:
            return plan
    # last resort: the whole text is the JSON object
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            return _as_plan(json.loads(stripped))
        except ValueError:
            return None
    return None


def _str_list(val) -> list[str]:
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, list):
        return []
    return [str(v).strip() for v in val if str(v).strip()]


def normalize_plan(plan: dict, default_title: str = "") -> dict:
    """Validate and clean a plan; raises PlanError if unusable.

    - 1..20 items with non-empty titles (extra items beyond 20 are dropped)
    - refs default to T1..Tn; dependencies may be given as refs ("T1") or 1-based ints
    - unknown/self dependencies are dropped here already (the server re-checks cycles)
    - priority clamped to 1..4 (default 2)
    """
    raw = plan.get("tasks") if isinstance(plan, dict) else None
    if not isinstance(raw, list) or not raw:
        raise PlanError("plan has no tasks")
    items: list[dict] = []
    for i, it in enumerate(raw[:MAX_ITEMS], 1):
        if not isinstance(it, dict):
            raise PlanError(f"task #{i} is not an object")
        title = str(it.get("title", "")).strip()
        if not title:
            raise PlanError(f"task #{i} has an empty title")
        ref = str(it.get("ref") or it.get("id") or it.get("key") or f"T{i}").strip()
        try:
            priority = int(it.get("priority", 2))
        except (TypeError, ValueError):
            priority = 2
        items.append({
            "ref": ref,
            "title": title[:200],
            "description": str(it.get("description", "")).strip(),
            "acceptance": _str_list(it.get("acceptance") or it.get("acceptance_criteria")),
            "depends_on": it.get("depends_on") or [],
            "priority": max(1, min(4, priority)),
            "agent_hint": str(it.get("agent_hint") or "").strip(),
        })
    # make refs unique, then resolve dependencies
    seen: set[str] = set()
    for i, it in enumerate(items, 1):
        if it["ref"] in seen:
            it["ref"] = f"T{i}"
        seen.add(it["ref"])
    refs = [it["ref"] for it in items]
    for it in items:
        deps: list[str] = []
        for d in it["depends_on"] if isinstance(it["depends_on"], list) else [it["depends_on"]]:
            if isinstance(d, int) and 1 <= d <= len(refs):
                d = refs[d - 1]
            d = str(d).strip()
            if d.isdigit() and 1 <= int(d) <= len(refs):
                d = refs[int(d) - 1]
            if d in refs and d != it["ref"] and d not in deps:
                deps.append(d)
        it["depends_on"] = deps
    return {
        "title": str(plan.get("title") or default_title).strip()[:120],
        "summary": str(plan.get("summary") or plan.get("rationale") or "").strip(),
        "tasks": items,
    }
