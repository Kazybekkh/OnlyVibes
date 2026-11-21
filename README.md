<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="web/logo/onlyvibes-dark.svg" />
    <img src="web/logo/onlyvibes.svg" alt="OnlyVibes" width="528" height="80" />
  </picture>
</h1>

A **Symphony-style agent orchestra over Tailscale**. You type a product prompt into the web board. An AI coding agent on one teammate's laptop breaks the product into a kanban board of tasks. Worker daemons on every laptop then claim those tasks and run their agent CLIs (Claude Code, Codex, Mistral Vibe, Cursor, or a mock) headless. The agents stream live transcripts and progress to the board, talk to each other across the network with the `ov` CLI, and push, open and merge PRs on a shared GitHub repo. The original live AR/3D view of the laptops is still there as a second tab.

```
 browser: web/index.html (kanban board) · web/ar.html (3D / WebXR table)
        │  HTTP /api/*  +  WS /board                     WS /view (3D)
        ▼
 ┌──────────── hub: server/ (FastAPI, one laptop) ────────────┐
 │ projects · tasks · workers · messages → server/data/        │
 │ scheduler: plan task first, then todo tasks by priority/deps│
 └─────────────────────────────────────────────────────────────┘
        ▲ WS /worker                       ▲ HTTP /api/agent/* (`ov say|inbox|progress|board|plan`)
 worker/worker.py on EACH laptop ── runs agent CLIs headless in per-task workspaces
 reporter/reporter.py (optional) ── passive process watcher for the 3D view (WS /ws)
```

The full contract (entities, statuses, WebSocket messages, HTTP API) is in [`shared/PROTOCOL.md`](shared/PROTOCOL.md).

## Repo layout

```
OnlyVibes/
├── shared/PROTOCOL.md     # the v2 contract shared by server, worker and web
├── shared/schema.json     # v1 reporter status message (3D view)
├── server/                # uv project — the hub (runs on ONE laptop)
│   ├── main.py            # FastAPI app: /api/*, WS /worker, WS /board, legacy /ws /view, static web/
│   ├── hub.py store.py scheduler.py planning.py peers.py legacy.py models.py config.py
│   └── tests/             # uv run pytest -q
├── worker/                # uv project — runs on EACH laptop
│   ├── worker.py          # daemon: connects to the hub, runs tasks, streams transcripts
│   ├── adapters.py        # claude-code / codex / vibe / cursor / mock → normalized events
│   ├── gitops.py          # workspace, branch, rebase/conflicts, push, gh pr create/merge
│   ├── prompts.py         # PLAN / BUILD / conflict-resolution prompts
│   ├── ov.py              # the `ov` CLI agents use to talk to each other
│   ├── mock_agent.py mock_fleet.py   # fake agents / fake laptops for demos
│   └── tests/
├── reporter/              # uv project — v1 passive watcher (optional)
└── web/
    ├── index.html board.js board.css lib.js demo.js   # kanban board (demo data when opened standalone)
    └── ar.html            # Three.js 3D table + WebXR "Enter AR"
```

## Quickstart (demo with mock agents, 2 minutes)

```bash
# terminal 1 — the hub
cd server && uv run uvicorn main:app --host 0.0.0.0 --port 8000

# terminal 2 — three fake laptops with mock agents
cd worker && MOCK_SPEED=5 SERVER_URL=ws://localhost:8000/worker uv run mock_fleet.py --n 3

# browser — open http://localhost:8000/, click "New product", leave the repo empty (local mode), launch
```

Opening `web/index.html` straight from disk (or adding `?demo=1`) runs an in-page simulation, so you can work on the UI without a hub.

## Real deployment over Tailscale

On the hub laptop (the team hub is `100.107.184.10`):

```bash
cd server
uv run uvicorn main:app --host 0.0.0.0 --port 8000
# optional: PUBLIC_URL=http://100.107.184.10:8000  OV_SHARED_SECRET=…  MISTRAL_API_KEY=…
# WebXR AR needs HTTPS:  tailscale serve 8000
```

On **each teammate's laptop** (needs `git`, `gh auth login` for GitHub mode, and at least one agent CLI installed and logged in):

```bash
cd worker
SERVER_URL=ws://100.107.184.10:8000/worker uv run worker.py
# AGENTS=claude-code,codex   pick adapters / preference order (default: auto-detect)
# MAX_CONCURRENT=2           tasks in parallel on this laptop
# optional, for the 3D tab:  cd reporter && SERVER_URL=ws://100.107.184.10:8000/ws uv run reporter.py
```

Then open `http://100.107.184.10:8000/` from any device on the Tailnet, and type what to build. If you give it a GitHub repo (`owner/name`), each task runs on its own `ov/<task>` branch and is squash-merged through a PR (`AUTO_MERGE=0` leaves PRs open in the Review column). Leave the repo empty for local mode, where each laptop commits to a local repo per project.

> Agents run **fully automatically** (`--dangerously-skip-permissions` / `danger-full-access`) inside `~/.onlyvibes/workspaces/<project>/…`. Only run workers on machines and repos you trust. The agent CLI also picks up your personal config (e.g. `~/.claude/CLAUDE.md`).

### How agents collaborate

Every agent run has an `ov` command on its PATH, scoped to its task by a per-task token:

```
ov board                       # who is doing what
ov inbox [--wait 30]           # new messages for me / my task / everyone
ov say "text" [--to T3|all]    # announce interfaces, ask or answer questions
ov progress 60 "API done"      # updates the card's progress bar
ov plan plan.json              # planner only: submit the task breakdown
```

Humans can join the conversation from the "Agent chatter" rail or from a task's Chat tab.

## Env vars

| Var               | Used by  | Default                    | Purpose                                             |
|-------------------|----------|----------------------------|-----------------------------------------------------|
| `SERVER_URL`      | reporter | `ws://localhost:8000/ws`   | websocket endpoint of the server's `/ws`            |
| `INTERVAL`        | reporter | `1`                        | seconds between status reports                      |
| `MISTRAL_API_KEY` | server   | *(unset)*                  | enables `summarize()` via Mistral; without it the stub returns the truncated raw log line |
| `MISTRAL_MODEL`   | server   | `mistral-small-latest`     | model used by `summarize()`                         |
| `PORT`            | server   | `8000`                     | listen port when run as `uv run main.py`            |
| `STALE_AFTER`     | server   | `5`                        | seconds before a silent laptop disappears from the view |
| `PUBLIC_URL`      | server   | *(Host header of the worker)* | base URL agents use to reach the hub's `/api/agent/*` |
| `OV_SHARED_SECRET`| server, worker | *(unset)*            | when set, `WS /worker` requires `?secret=…` (workers append it) |
| `OV_DATA_DIR`     | server   | `server/data`              | where projects, tasks and transcripts are persisted |
| `SERVER_URL`      | worker   | `ws://localhost:8000/worker` | hub websocket                                     |
| `WORKER_ID`       | worker   | hostname                   | unique worker id (set it if two laptops share a hostname) |
| `AGENTS`          | worker   | auto-detect                | adapters in preference order: `claude-code,codex,vibe,cursor,mock` |
| `MAX_CONCURRENT`  | worker   | `1`                        | tasks run in parallel on this laptop                |
| `WORKSPACES_DIR`  | worker   | `~/.onlyvibes/workspaces`  | per-task workspaces                                 |
| `AUTO_MERGE`      | worker   | `1`                        | squash-merge PRs automatically; `0` leaves them in Review |
| `AGENT_TIMEOUT`   | worker   | `3600`                     | max seconds per agent run                           |
| `MOCK_SPEED`      | worker   | `1`                        | speed multiplier for mock agents                    |
| `VOICE_MIC`       | server   | `browser`                  | which role may talk: `browser` (Zapbox headset phone) or `web` (fallback `/mic` page) |
| `VOICE_SPEAKER`   | server   | `browser`                  | where replies are spoken: `browser`, `web`, `mic` (same as mic), or `all` |
| `VOICE_CHAT_MODEL`| server   | `MISTRAL_MODEL`            | LLM behind `/api/voice/chat` (grounded in the live snapshot) |
| `MISTRAL_API_BASE`| server   | `https://api.mistral.ai/v1`| Mistral API base (token mint + chat + SSE)              |

All worker variables are documented in `worker/worker.py`'s docstring, and all server variables in `server/main.py`'s.

## Voice prompts in Zapbox (AR)

Voice is for interrupting one person's agent and giving it a new prompt. Voxtral is used only for
speech-to-text (no spoken replies, no Q&A).

1. Open `https://<hub>/ar?key=<OV_CONTROL_SECRET>` in Zapbox Browser, tap **🎙 Enable voice**, then **Enter AR**.
2. Point at an agent card and pull the **trigger**: it highlights and the head-locked card says
   "Voice target: <agent> on <owner>'s laptop".
3. **Hold B/Y** (xr-standard gamepad button 5), speak the new prompt, release.
4. The transcript shows for 2 s ("press B/Y to cancel"), then `POST /api/control` `action=redirect` interrupts that agent
   and sends the transcript as its new prompt. With no card selected it just says "Select an agent first".

- What redirect does (reporter whitelist): Claude Code inside tmux gets Esc + the prompt typed into its pane (session
  stays open); Claude Code in any other terminal gets Ctrl-C (it exits) and then `claude -p --resume <session> "<prompt>"`
  continues the same conversation headless; orchestra task cards get the text in their inbox; other agents (codex, vibe,
  cursor...) are interrupted only, and the card says they can't take a voice prompt yet.
- Buttons: A/X and stick click stay "Place laptops", trigger selects cards, squeeze summons the scene. Desktop / phone:
  click a card, then the **Hold to talk** button or hold **Space**. `?ptt=<button index>` or `?ptt=trigger` overrides.
- Fallback mic: `https://<hub>/mic?key=…` on a laptop or phone: pick the agent in the dropdown, hold to talk, same redirect.
- The browser never sees the Mistral key: `/api/voice/token` (control key required) mints a short-lived `rt_*` token for
  `voxtral-mini-transcribe-realtime-3` and the page streams audio to Mistral directly. Probe page: `/probe`.

## v1 reporter schema (`shared/schema.json`, 3D tab)

One status message per laptop per second, sent to `WS /ws`:

```json
{
  "laptop": "kay-mbp",
  "owner": "kay",
  "tailscale_ip": "100.64.0.11",
  "ts": 1761998400.12,
  "agents": [
    {
      "name": "vibe",
      "pid": 3001,
      "cpu": 42.1,
      "cwd": "/Users/kay/Developer/OnlyVibes",
      "last_log": "⏺ Edit server/main.py",
      "summary": "editing the FastAPI server"
    }
  ]
}
```

- `reporter` fills everything except `summary` (sent empty).
- The server fills `summary` via `summarize()` (Mistral Small: one plain-English line from `last_log`), stores the latest message per `tailscale_ip`, and rebroadcasts all states on `WS /view` as `{"type":"state","ts":...,"laptops":[...]}` at 2 Hz.
- `/view` also includes one synthesized laptop per connected worker, showing its current task.
- `web/ar.html` renders one box per laptop on a 3D table and a floating text card per agent.

## WebXR / AR notes

- WebXR requires a **secure context**: `localhost` or HTTPS. On Tailscale use `tailscale serve 8000` on the hub and open the `https://....ts.net` URL on your headset/phone.
- The **📱 Enter AR** button requests an `immersive-ar` session (with `hit-test` + `local-floor`); on unsupported browsers it stays hidden and the desktop 3D view keeps working.
- Opening `web/ar.html` standalone (no server) shows fake data after ~2.5s, so the frontend can be developed alone.

## Hackathon division of labor

| Person | Owns | Runs alone via |
|--------|------|----------------|
| 1 | `shared/PROTOCOL.md` + README | — |
| 2 | `worker/` (+ `reporter/`) | `cd worker && uv run pytest -q`, `AGENTS=mock uv run worker.py` |
| 3 | `server/` | `cd server && uv run pytest -q`, `uv run uvicorn main:app --port 8000` |
| 4 | `web/` | open `web/index.html` in a browser (demo data), or against a hub + `mock_fleet.py` |
