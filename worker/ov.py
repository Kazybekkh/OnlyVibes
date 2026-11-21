#!/usr/bin/env python3
"""
ov — the OnlyVibes teammate CLI used by AI agents *inside* a task run.

The worker installs a tiny `ov` shim on the agent's PATH, so an agent can talk
to the hub and to the other agents working on the same project:

    ov say "text" [--to all|<task id>|<worker id>] [--reply-to m-…]
    ov inbox [--wait 30] [--json]   # new messages since the last call
    ov progress <0-100> "note"
    ov board [--json]               # who is doing what
    ov plan <plan.json>             # planner only: submit the task breakdown

Stdlib only (urllib, json, argparse). Exit code 0 on success; errors go to
stderr with exit code 1. A hard alarm guarantees it never runs longer than
`wait + 10` seconds.

Env vars (set by the worker for every agent run):
    OV_SERVER     hub HTTP base URL, e.g. http://100.107.184.10:8000
    OV_TOKEN      per-task bearer token for /api/agent/*
    OV_TASK_ID    this task's id (used for the default cursor file name)
    OV_STATE_DIR  where the inbox cursor (and a copy of a submitted plan) is kept
                  (default ~/.onlyvibes)

Run directly (for debugging):
    OV_SERVER=http://localhost:8765 OV_TOKEN=... python ov.py board
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_WAIT = 60
SLACK = 10  # seconds of headroom on top of --wait before we give up for good


class OvError(Exception):
    pass


# ---------------------------------------------------------------- plumbing


def _env(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "").strip()
    if required and not val:
        raise OvError(f"{name} is not set (ov must run inside an OnlyVibes task)")
    return val


def _state_dir() -> str:
    d = os.environ.get("OV_STATE_DIR") or os.path.expanduser("~/.onlyvibes")
    os.makedirs(d, exist_ok=True)
    return d


def _cursor_path() -> str:
    explicit = os.environ.get("OV_STATE_DIR")
    if explicit:
        return os.path.join(_state_dir(), "inbox.cursor")
    task = os.environ.get("OV_TASK_ID", "default").replace("/", "_")
    return os.path.join(_state_dir(), f"ov-{task}.cursor")


def request(method: str, path: str, body: dict | None = None,
            query: dict | None = None, timeout: float = 8) -> dict:
    """One JSON request against the hub's agent API; raises OvError on failure."""
    base = _env("OV_SERVER").rstrip("/")
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_env('OV_TOKEN')}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            payload = json.loads(e.read() or b"{}")
            err = payload.get("error") or payload.get("detail") or payload
            detail = err.get("message", json.dumps(err)) if isinstance(err, dict) else str(err)
        except Exception:
            pass
        raise OvError(f"{method} {path} failed: HTTP {e.code} {detail}".strip())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise OvError(f"cannot reach the OnlyVibes hub at {base}: {reason}")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise OvError(f"{method} {path}: hub returned non-JSON response")


def _who(msg: dict) -> str:
    frm = msg.get("from") or {}
    if isinstance(frm, str):
        return frm
    return frm.get("name") or frm.get("task_id") or frm.get("kind") or "?"


def format_message(msg: dict) -> str:
    ts = msg.get("ts")
    clock = time.strftime("%H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) and ts else "--:--:--"
    reply = f" (re {msg['reply_to']})" if msg.get("reply_to") else ""
    frm = msg.get("from") or {}
    sender_task = frm.get("task_id") if isinstance(frm, dict) else None
    if sender_task:
        hint = f" [reply with: ov say --to {sender_task} --reply-to {msg.get('id')} \"...\"]"
    elif (frm.get("kind") if isinstance(frm, dict) else "") == "human":
        # humans have no task id: a reply goes to the whole project, where the board shows it
        hint = f" [reply with: ov say --reply-to {msg.get('id')} \"...\"]"
    else:
        hint = ""
    return (f"[{msg.get('id', '?')} {clock}] {_who(msg)} -> {msg.get('to', 'all')}{reply}:\n"
            f"  {msg.get('text', '')}{hint}")


# ---------------------------------------------------------------- commands


def cmd_say(args) -> int:
    to = args.to or "all"
    if to == os.environ.get("OV_TASK_ID"):
        # agents answering a human sometimes address themselves; nobody would read that
        print("ov: --to is your own task id; sending to all instead", file=sys.stderr)
        to = "all"
    body: dict = {"text": args.text, "to": to}
    if args.reply_to:
        body["reply_to"] = args.reply_to
    msg = request("POST", "/api/agent/say", body)
    print(f"sent {msg.get('id', '')} to {body['to']}".strip())
    return 0


def cmd_inbox(args) -> int:
    wait = max(0, min(int(args.wait), MAX_WAIT))
    cursor_file = _cursor_path()
    since = ""
    if os.path.exists(cursor_file):
        with open(cursor_file) as fh:
            since = fh.read().strip()
    res = request("GET", "/api/agent/inbox", query={"since": since, "wait": wait},
                  timeout=wait + SLACK - 2)
    messages = res.get("messages") or []
    cursor = res.get("cursor") or (messages[-1].get("id") if messages else since)
    if cursor and cursor != since:
        with open(cursor_file, "w") as fh:
            fh.write(str(cursor))
    if args.json:
        print(json.dumps({"messages": messages, "cursor": cursor}, indent=2))
        return 0
    if not messages:
        print("no new messages")
        return 0
    for m in messages:
        print(format_message(m))
    return 0


def cmd_progress(args) -> int:
    try:
        pct = int(float(args.percent))
    except ValueError:
        raise OvError(f"progress must be a number 0-100, got {args.percent!r}")
    pct = max(0, min(100, pct))
    request("POST", "/api/agent/progress", {"progress": pct, "note": " ".join(args.note)})
    print(f"progress {pct}%")
    return 0


def format_board(res: dict) -> str:
    project = res.get("project") or {}
    tasks = res.get("tasks") or []
    lines = [f"Project {project.get('id', '?')}: {project.get('title') or project.get('prompt', '')}".rstrip(),
             f"repo: {project.get('repo') or '(local mode)'}  status: {project.get('status', '?')}", ""]
    me = os.environ.get("OV_TASK_ID", "")
    for t in tasks:
        a = t.get("assignee") or {}
        who = f"{a.get('agent', '?')}@{a.get('laptop') or a.get('worker_id', '?')}" if a else "unassigned"
        deps = ",".join(d.rsplit("-", 1)[-1] for d in t.get("depends_on") or [])
        mark = "*" if t.get("id") == me else " "
        lines.append(f"{mark}{t.get('key', '?'):>5}  {t.get('status', '?'):<11} {t.get('progress', 0):>3}%  "
                     f"{t.get('title', '')}  [{who}]" + (f"  deps:{deps}" if deps else ""))
        extra = t.get("progress_note") or ""
        if t.get("branch"):
            extra += ("  " if extra else "") + f"branch {t['branch']}"
        if extra:
            lines.append(f"{'':7}{extra}")
        lines.append(f"{'':7}id: {t.get('id', '')}")
    lines.append("")
    lines.append("(* = you)  message a teammate: ov say --to <id> \"...\"")
    return "\n".join(lines)


def cmd_board(args) -> int:
    res = request("GET", "/api/agent/board")
    print(json.dumps(res, indent=2) if args.json else format_board(res))
    return 0


def load_plan_file(path: str) -> dict:
    try:
        with open(path) as fh:
            plan = json.load(fh)
    except FileNotFoundError:
        raise OvError(f"no such file: {path}")
    except ValueError as e:
        raise OvError(f"{path} is not valid JSON: {e}")
    if isinstance(plan, list):
        plan = {"title": "", "summary": "", "tasks": plan}
    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list) or not plan["tasks"]:
        raise OvError("plan must be {\"title\", \"summary\", \"tasks\": [PlanItem, ...]} with at least one task")
    for i, item in enumerate(plan["tasks"], 1):
        if not isinstance(item, dict) or not str(item.get("title", "")).strip():
            raise OvError(f"task #{i} needs a non-empty title")
    return plan


def cmd_plan(args) -> int:
    plan = load_plan_file(args.file)
    # Keep a local copy: the worker picks it up after the run (and submits it
    # itself via plan_result if posting to the hub below did not work).
    state = _state_dir()
    with open(os.path.join(state, "plan.json"), "w") as fh:
        json.dump(plan, fh, indent=2)
    marker = os.path.join(state, "plan.submitted")
    try:
        request("POST", "/api/agent/plan", plan)
    except OvError as e:
        print(f"warning: could not submit to the hub ({e}); "
              f"saved locally, the worker will submit it when you finish", file=sys.stderr)
        print(f"plan with {len(plan['tasks'])} tasks saved")
        return 0
    with open(marker, "w") as fh:
        fh.write(str(time.time()))
    print(f"plan with {len(plan['tasks'])} tasks submitted")
    return 0


# ---------------------------------------------------------------- entry


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ov", description="Talk to the OnlyVibes hub and your teammate agents.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("say", help="post a message to the project chat")
    s.add_argument("text")
    s.add_argument("--to", default="all", help="all | <task id> | <worker id>")
    s.add_argument("--reply-to", dest="reply_to", default=None)
    s.set_defaults(fn=cmd_say)

    s = sub.add_parser("inbox", help="print new messages since the last call")
    s.add_argument("--wait", type=int, default=0, help=f"long-poll up to N seconds (max {MAX_WAIT})")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_inbox)

    s = sub.add_parser("progress", help="report progress 0-100 with a one-line note")
    s.add_argument("percent")
    s.add_argument("note", nargs="*")
    s.set_defaults(fn=cmd_progress)

    s = sub.add_parser("board", help="show every task of the project and who works on it")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_board)

    s = sub.add_parser("plan", help="(planner only) submit a plan JSON file")
    s.add_argument("file")
    s.set_defaults(fn=cmd_plan)
    return p


def _on_alarm(_sig, _frm):
    print("ov: timed out talking to the hub", file=sys.stderr)
    os._exit(1)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    wait = max(0, min(int(getattr(args, "wait", 0) or 0), MAX_WAIT))
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(wait + SLACK)
    try:
        return args.fn(args)
    except OvError as e:
        print(f"ov: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 1
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
