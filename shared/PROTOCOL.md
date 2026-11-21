# OnlyVibes Orchestra — protocol contract (v2)

This file is the **single source of truth** shared by `server/`, `worker/` and `web/`.
Any component may be developed alone against it. Field names, message types and
status strings below are exact — do not rename them.

```
 browser (web/index.html board, web/ar.html 3D)
        │  HTTP /api/*  +  WS /board (live)        WS /view (legacy 3D snapshot)
        ▼
 ┌────────────── hub: server/ (FastAPI, one laptop) ──────────────┐
 │ store (projects, tasks, workers, messages) → server/data/*.json │
 │ scheduler loop: assigns plan/build tasks to free worker slots   │
 └─────────────────────────────────────────────────────────────────┘
        ▲  WS /worker (JSON msgs)            ▲ HTTP /api/agent/* (the `ov` CLI)
        │                                     │
 worker/worker.py on EACH laptop ── spawns agent CLIs headless ── agents call `ov ...`
   adapters: claude-code, codex, vibe, cursor, mock
   (existing reporter/ + WS /ws stay as the passive process watcher)
```

Hub in the team deployment: `100.107.184.10:8000` (Kazybek's MacBook over Tailscale).
Workers connect with `SERVER_URL=ws://100.107.184.10:8000/worker`.
All timestamps are float UNIX seconds. All ids are strings.

---

## 1. Entities

### Project
```json
{
  "id": "p-3f9a",               // "p-" + 4 hex chars
  "prompt": "Build a pomodoro web app with stats",
  "title": "Pomodoro app",      // short name; planner fills it, server default = first 6 words of prompt
  "repo": "acme/pomodoro",     // GitHub "owner/name"; "" = local mode (no git push / PR / merge)
  "base_branch": "main",
  "status": "planning",         // planning | active | done | failed
  "created_at": 1795262653.9,
  "plan_task_id": "p-3f9a-PLAN",
  "summary": ""                  // planner's one-paragraph breakdown rationale
}
```
Project status rules (server-computed): `planning` while its plan task is not `done`;
`failed` if the plan task is `failed`; `active` once build tasks exist; `done` when every
build task is `done` or `cancelled` (and at least one is `done`).

### Task
```json
{
  "id": "p-3f9a-T3",            // plan task: "<project>-PLAN"; build tasks: "<project>-T<n>", n from 1
  "key": "T3",                   // short display id ("PLAN" for the plan task)
  "project_id": "p-3f9a",
  "kind": "build",               // plan | build
  "title": "Timer state machine + persistence",
  "description": "markdown: what to build, files/areas, interfaces to honour",
  "acceptance": ["timer survives reload", "unit tests pass"],
  "depends_on": ["p-3f9a-T1"],   // task ids; must be done before this leaves backlog
  "priority": 2,                 // 1 (urgent) .. 4 (low)
  "agent_hint": "",              // optional preferred adapter name, e.g. "codex"; "" = any
  "status": "todo",              // see §2
  "assignee": null,              // or {"worker_id","laptop","owner","agent"} while/after running
  "progress": 0,                 // 0..100
  "progress_note": "",           // latest one-line note from `ov progress` / worker
  "last_event": "",              // one-line text of the most recent transcript event
  "branch": "",                  // e.g. "ov/p-3f9a-T3-timer-state-machine"
  "pr_url": "",
  "result": "",                  // final summary written by the agent/worker
  "error": "",
  "attempts": 0,
  "created_at": 0.0, "started_at": null, "finished_at": null, "updated_at": 0.0,
  "tokens": {"input": 0, "output": 0},
  "event_count": 0               // number of transcript events stored (events are NOT inlined, see GET /api/tasks/{id})
}
```

### Transcript event (per task, append-only, server keeps the last 3000)
```json
{"seq": 17, "ts": 1795262700.1, "kind": "tool_call", "text": "Bash: npm test", "tool": "Bash", "data": {}}
```
`kind` ∈ `system` (worker/lifecycle lines) · `user` (prompt sent to the agent) ·
`assistant` (agent prose) · `thinking` · `tool_call` · `tool_result` · `error` · `git` (push/PR/merge steps).
`text` is human-readable and ≤ 4000 chars (truncate with "…"). `data` is optional raw extra (≤ 8 KB).
`seq` is assigned by the server (monotonic per task, from 1).

### Worker (one per laptop process)
```json
{
  "id": "endermite",            // WORKER_ID env, default = hostname (lowercased, no ".local")
  "laptop": "endermite", "owner": "carlo", "tailscale_ip": "100.124.163.102",
  "agents": ["claude-code", "codex"],   // adapters actually available on this laptop
  "max_concurrent": 1,
  "running": ["p-3f9a-T3"],     // task ids currently executing here
  "status": "busy",             // idle | busy | offline   (server-computed)
  "connected_at": 0.0, "last_seen": 0.0, "version": "2"
}
```
A worker is `offline` after 15 s without any message; it stays listed for the UI.

### Message (inter-agent / human chat)
```json
{
  "id": "m-000042", "project_id": "p-3f9a", "ts": 0.0,
  "from": {"kind": "agent", "task_id": "p-3f9a-T3", "worker_id": "endermite", "agent": "claude-code", "name": "T3 · claude-code@endermite"},
  "to": "all",                   // "all" | a task id | a worker id
  "text": "I'm exposing useTimer() from src/timer.ts — T4 please import it from there",
  "reply_to": null               // optional message id
}
```
`from.kind` ∈ `agent` | `human` | `system`. Human messages: `{"kind":"human","name":"carlo"}`.

---

## 2. Task status (kanban columns)

| status        | column        | meaning |
|---------------|---------------|---------|
| `backlog`     | Backlog       | has unfinished `depends_on` |
| `todo`        | Todo          | ready, unassigned |
| `claimed`     | In Progress   | assigned to a worker, `run_task` sent, not yet started |
| `in_progress` | In Progress   | agent running |
| `review`      | Review        | agent finished, worker doing git push / PR / merge, or PR left open for a human |
| `done`        | Done          | merged (or local-mode finished) |
| `failed`      | Done column, red | gave up (shown with Retry button) |
| `cancelled`   | Done column, grey | cancelled by a human |

Server rules:
- New build tasks start `backlog` if any dependency isn't `done`, else `todo`. Whenever a task becomes
  `done`, re-evaluate backlog tasks. Dependencies on `cancelled` tasks count as satisfied; on `failed` do not.
- Scheduler (every 1 s, and immediately after any state change): for each connected worker with
  `len(running) < max_concurrent`, pick the next task: plan tasks first, then `todo` build tasks sorted by
  `priority` asc, `created_at` asc, `key` natural order. Skip a task whose `agent_hint` isn't in the
  worker's `agents` **unless** no connected worker has that adapter. Prefer, for a worker, the task whose
  dependencies it executed itself (locality) when priorities tie. Then set `status=claimed`, `assignee`,
  `attempts += 1`, and send `run_task`. Agent choice on the worker: `agent_hint` if available, else the
  first of the worker's `agents` (the worker orders them by preference).
- If a worker disconnects or goes offline, its `claimed`/`in_progress` tasks go back to `todo`
  (or `failed` after 3 attempts) with a `system` event explaining why.
- If `claimed` for > 30 s without `task_started`, revert to `todo`.

---

## 3. WebSocket `/worker` (worker ⇄ server)

Every frame is one JSON object with a `type`. Unknown types are ignored (log once).

### worker → server
| type | fields |
|------|--------|
| `hello` | `worker` (Worker object minus server-computed fields: id, laptop, owner, tailscale_ip, agents, max_concurrent, version), `running` (task ids it is still executing from before a reconnect) |
| `heartbeat` | `running` (task ids) — every 5 s |
| `task_started` | `task_id`, `agent`, `branch` (may be ""), `workspace` (local path, informational) |
| `task_event` | `task_id`, `event` (§1 event without `seq`) — the worker may batch: `events` (list) instead of `event` |
| `task_progress` | `task_id`, `progress` (0-100), `note` |
| `task_usage` | `task_id`, `tokens` {input, output} (absolute totals for this attempt) |
| `task_status` | `task_id`, `status` = `review` (worker started git push/PR), `note` |
| `task_finished` | `task_id`, `ok` (bool), `result` (summary), `error`, `branch`, `pr_url`, `merged` (bool) |
| `plan_result` | `task_id` (the PLAN task), `project_id`, `title`, `summary`, `tasks` (list of PlanItem, §5) |

`task_finished` semantics for build tasks: `ok && (merged || repo == "")` → `done`;
`ok && !merged` → `review` (PR open for a human; UI offers "Mark done"); `!ok` → `todo` for retry if
`attempts < 3`, else `failed`. For the plan task, `plan_result` is what completes it (server sets it
`done` and creates build tasks); `task_finished` with `ok=false` on a plan task → retry/failed as above.

### server → worker
| type | fields |
|------|--------|
| `welcome` | `worker_id`, `server_version`, `server_time` |
| `run_task` | `task` (Task), `project` (Project), `board` (list of `{id,key,title,status,assignee,depends_on}` for all tasks in the project), `agent` (adapter to use), `server_http` (base URL agents should use for `ov`, e.g. `http://100.107.184.10:8000`), `token` (per-task bearer token for `/api/agent/*`) |
| `cancel_task` | `task_id`, `reason` |
| `chat` | `message` (Message) — forwarded when `to` is one of this worker's running task ids, its worker id, or `all` for a project it runs; the worker may inject it into the agent if the adapter supports live input, otherwise ignore (agents poll `ov inbox`) |

---

## 4. HTTP API (server)

### Board / humans
- `GET  /api/state` → `{projects, tasks, workers, messages}` (tasks without events; last 300 messages). Also keeps
  the legacy `laptops` key for the old reporter snapshot.
- `POST /api/projects` `{prompt, repo?, base_branch?}` → Project. Creates the project and its PLAN task (`kind=plan`, `status=todo`, priority 0).
- `GET  /api/projects/{id}` → `{project, tasks}`
- `GET  /api/tasks/{id}` → `{task, events}` (all stored events)
- `POST /api/projects/{id}/tasks` `{title, description?, acceptance?, depends_on?, priority?, agent_hint?}` → Task (human-added)
- `POST /api/tasks/{id}/retry` → task back to `todo` (attempts reset to 0, error cleared)
- `POST /api/tasks/{id}/cancel` → `cancelled`; if running, server sends `cancel_task`
- `POST /api/tasks/{id}/done` → `done` (human accepted an open PR)
- `POST /api/messages` `{project_id, to?, text, name?}` → Message from a human
- `GET  /api/workers` → Worker list
- errors: `{"error": {"code": "...", "message": "..."}}` with 4xx

### Agent-facing (used by the `ov` CLI inside agent runs)
Auth: `Authorization: Bearer <token>` from `run_task.token`; the token identifies the task (and thus project, worker, agent).
- `POST /api/agent/say` `{text, to?="all", reply_to?}` → Message
- `GET  /api/agent/inbox?since=<message id or empty>&wait=<sec, max 60>` → `{messages: [...], cursor}` — messages in
  the project addressed to `all`, this task id, or this worker id, excluding the caller's own; `wait` long-polls.
- `POST /api/agent/progress` `{progress, note}`
- `GET  /api/agent/board` → `{project, tasks}` (compact, incl. status/assignee/progress_note/branch)
- `POST /api/agent/plan` `{title, summary, tasks: [PlanItem]}` — alternative way for a planner agent to submit its
  plan directly (equivalent to the worker sending `plan_result`).

### WebSocket `/board` (browser, server → client only)
On connect: `{"type":"snapshot", "projects", "tasks", "workers", "messages"}`. Then incremental frames:
`{"type":"project","project"}` · `{"type":"task","task"}` (full task, no events) ·
`{"type":"event","task_id","event"}` · `{"type":"message","message"}` · `{"type":"worker","worker"}` ·
`{"type":"ping","ts"}` every 15 s. Clients may reconnect any time and get a fresh snapshot.

### Legacy (unchanged behaviour)
`WS /ws` (reporter ingest), `WS /view` (3D snapshot at 2 Hz), `POST /summarize`. `/view`'s `laptops` list must also
include one synthesized entry per connected worker: `{laptop, owner, tailscale_ip, ts, agents: [{name: agent, pid: 0,
cpu: 0, cwd: "", last_log: task.last_event, summary: "<key> · <title> — <progress>%"}]}` (merged with a reporter entry
for the same tailscale_ip if one exists). Static: `GET /` → `web/index.html` (board), `GET /ar` → `web/ar.html`.

---

## 5. Planning

PLAN task prompt (worker builds it; see worker/prompts.py) asks the agent to inspect the repo (if any), decompose
the product into **4–12 build tasks** that different agents on different laptops can execute **in parallel**, with
explicit interfaces between them, a scaffold/foundation task first that others depend on when the repo is empty,
and to output the plan as JSON. Submission: the agent runs `ov plan <file.json>` (→ `POST /api/agent/plan`) **or**
ends its final message with a fenced ```json block; the worker parses whichever arrives and sends `plan_result`.

PlanItem:
```json
{"ref": "T1", "title": "...", "description": "...", "acceptance": ["..."], "depends_on": ["T1"], "priority": 2, "agent_hint": ""}
```
`ref`s are local to the plan (T1..Tn); the server maps them to `<project>-T<n>` ids in order and drops unknown /
self / cyclic dependencies. Plan output is validated: 1–20 items, non-empty titles.

Planning in GitHub mode also bootstraps the repo: if `repo` has no `base_branch` yet (empty repo), the planner's worker
creates it with an initial commit (README.md containing the product prompt + plan) and pushes before sending `plan_result`.

---

## 6. Worker execution (build task, GitHub mode)

1. Workspace `~/.onlyvibes/workspaces/<project_id>/<task key>`; `gh repo clone <repo>` (or `git fetch` if present),
   `git checkout -B <branch> origin/<base_branch>`. Branch = `ov/<task id lowercased>-<slug of title, ≤40 chars>`.
2. Spawn the adapter with the task prompt (task + project + board + teammates + `ov` usage + rules: stay in the
   workspace, commit work with clear messages, don't push — the worker pushes). Env for the agent:
   `OV_SERVER` (http base), `OV_TOKEN`, `OV_TASK_ID`, `OV_PROJECT_ID`, `OV_WORKER_ID`, and `PATH` prefixed with a dir containing `ov`.
3. Stream normalized events (`task_event`), usage (`task_usage`).
4. On agent exit 0: commit leftovers (`git add -A && git commit -m "<key>: <title> (wip leftovers)"` if dirty);
   `task_status review`; `git fetch && git rebase origin/<base>` — on conflict `git rebase --abort`, `git merge origin/<base>`,
   and re-run the agent once with a "resolve the merge conflicts, run the tests, commit" prompt; then `git push -u origin <branch>`,
   `gh pr create --base <base> --head <branch> --title "<key>: <title>" --body <result + acceptance>`, and if `AUTO_MERGE=1`
   (default) `gh pr merge <url> --squash --delete-branch` (retry once after re-sync on failure).
   Send `task_finished` with `pr_url`, `merged`.
5. Local mode (`repo == ""`): workspace `~/.onlyvibes/workspaces/<project_id>/shared` (one dir per project per laptop,
   `git init` once, tasks commit there); no push/PR; `task_finished ok merged=false` ⇒ server marks `done`.
6. Agent exit ≠ 0, crash, or `cancel_task` (kill the process group) → `task_finished ok=false error=...`.
7. If a task produced no commits and no changes, it's still `ok` (some tasks are verification-only); note it in `result`.

Adapters (worker/adapters.py) — each turns one CLI's streaming output into §1 events:
- `claude-code`: `claude -p <prompt> --output-format stream-json --verbose --dangerously-skip-permissions` (cwd = workspace)
- `codex`: `codex exec --json --sandbox danger-full-access --skip-git-repo-check <prompt>` — verify flags with `codex exec --help`; parse JSONL events
- `vibe`: Mistral Vibe CLI in programmatic mode (`vibe --prompt <prompt> --auto-approve` or as available); plain-text lines → `assistant` events if no JSON mode
- `cursor`: `cursor-agent -p <prompt> --output-format stream-json --force`
- `mock`: no CLI; simulates ~20–60 s of realistic events, calls the agent API like a real agent would (`progress`, `say`, sometimes reads `inbox`), writes a small file + commits in the workspace, and for PLAN tasks returns a canned 5–7 item plan derived from the prompt words. Used for demos/tests.
Availability = executable on PATH (`shutil.which`) — `mock` only when `AGENTS` env includes it.
`AGENTS` env (comma list) overrides detection and sets preference order.

---

## 7. The `ov` CLI (worker/ov.py, stdlib only, installed into the agent's PATH as `ov`)
```
ov say "text" [--to all|<task id>|<worker id>] [--reply-to m-…]
ov inbox [--wait 30]            # prints new messages since last call (cursor kept in $OV_STATE_DIR or ~/.onlyvibes/ov-<task>.cursor)
ov progress <0-100> "note"
ov board                        # who is doing what
ov plan <plan.json>             # planner only
```
Exit code 0 on success; prints errors to stderr and exits 1 (never hangs > wait+10 s).
