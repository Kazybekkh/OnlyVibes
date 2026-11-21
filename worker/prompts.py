"""
Prompt templates for agent runs (PROTOCOL.md §5, §6).

    render_plan_prompt(...)      PLAN task: decompose a product prompt into 4-12 parallel build tasks
    render_build_prompt(...)     build task: implement one card of the board as part of a team
    render_conflict_prompt(...)  re-run after `git merge origin/<base>` left conflicts

Pure functions over plain dicts (Task / Project / board entries as in the
protocol), so they are easy to unit-test and to tweak. The wording matters:
these prompts decide how well a fleet of independent agents on different
laptops cooperates without stepping on each other.
"""
from __future__ import annotations

import json

KNOWN_AGENTS = ["claude-code", "codex", "vibe", "cursor"]


# ---------------------------------------------------------------- helpers


def _bullets(items, empty: str = "- (none)") -> str:
    items = [str(i).strip() for i in items or [] if str(i).strip()]
    return "\n".join(f"- {i}" for i in items) if items else empty


def _who(assignee: dict | None) -> str:
    if not assignee:
        return "unassigned"
    agent = assignee.get("agent") or "agent"
    where = assignee.get("laptop") or assignee.get("worker_id") or "?"
    owner = f" ({assignee['owner']})" if assignee.get("owner") else ""
    return f"{agent}@{where}{owner}"


def render_board(board: list[dict], me: str = "") -> str:
    """Compact table of every task in the project, marking the current one."""
    if not board:
        return "(the board is empty)"
    keys = {b.get("id"): b.get("key", "?") for b in board}
    lines = []
    for b in board:
        deps = ", ".join(keys.get(d, d) for d in b.get("depends_on") or [])
        mark = "  <== YOU" if b.get("id") == me else ""
        lines.append(f"- {b.get('key', '?')} [{b.get('status', '?')}] {b.get('title', '')}"
                     f" — {_who(b.get('assignee'))} — id `{b.get('id', '')}`"
                     + (f" — depends on {deps}" if deps else "") + mark)
    return "\n".join(lines)


def _relations(task: dict, board: list[dict]) -> str:
    """Who this task depends on, who depends on it, who runs in parallel."""
    me = task.get("id")
    by_id = {b.get("id"): b for b in board}
    deps = [by_id[d] for d in task.get("depends_on") or [] if d in by_id]
    dependents = [b for b in board if me in (b.get("depends_on") or [])]
    others = [b for b in board if b.get("id") != me and b not in deps and b not in dependents
              and b.get("status") in ("claimed", "in_progress", "review", "todo", "backlog")]

    def fmt(b):
        return f"{b.get('key')} `{b.get('id')}` {b.get('title', '')} [{b.get('status')}, {_who(b.get('assignee'))}]"

    out = []
    out.append("Built before you (their code is already on the base branch when they are `done` — build on it, "
               "don't redo it):\n" + _bullets([fmt(b) for b in deps]))
    out.append("Waiting on YOU (they will import/use what you create — keep the interfaces you were asked "
               "to provide exactly, and tell them if anything changes):\n" + _bullets([fmt(b) for b in dependents]))
    out.append("Working in parallel right now / soon (avoid their files):\n" + _bullets([fmt(b) for b in others]))
    return "\n\n".join(out)


OV_HELP = """\
`ov` is already on your PATH. It is how you talk to the hub and to the other agents:
  ov inbox                       new messages for you (questions, interface announcements, human notes)
  ov inbox --wait 30             same, but wait up to 30 s for something to arrive
  ov board                       every task of the project: status, assignee, progress, ids
  ov progress <0-100> "note"     update your card on the board (short, concrete note)
  ov say "text"                  message everyone on the project
  ov say --to <task id> "text"   message the agent working on one task (ids are on the board)
  ov say --to <task id> --reply-to <message id> "text"   answer an agent's message
  ov say --reply-to <message id> "text"                  answer a human (humans have no task id)
Put message text in 'single quotes' when it contains `backticks` or $vars — inside double quotes the
shell runs them before ov sees the text."""


# ---------------------------------------------------------------- PLAN


PLAN_SCHEMA_EXAMPLE = {
    "title": "Pomodoro app",
    "summary": "Vite + TypeScript SPA. T1 scaffolds the app shell and shared types; timer logic, "
               "stats storage and UI panels are built in parallel against those types; T6 integrates.",
    "tasks": [
        {
            "ref": "T1",
            "title": "Scaffold: Vite+TS app shell, shared types, test setup",
            "description": "Create the project skeleton ... OWNS: package.json, vite.config.ts, src/main.ts, "
                           "src/types.ts, README.md. INTERFACES: export `type Session = {...}` from src/types.ts ...",
            "acceptance": ["`npm test` runs (zero tests is fine)", "`npm run build` succeeds",
                           "src/types.ts exports Session and Settings"],
            "depends_on": [],
            "priority": 1,
            "agent_hint": "",
        },
        {
            "ref": "T2",
            "title": "Timer state machine",
            "description": "OWNS: src/timer/*. Implements `createTimer(settings: Settings): Timer` ...",
            "acceptance": ["unit tests cover start/pause/reset/phase switch"],
            "depends_on": ["T1"],
            "priority": 2,
            "agent_hint": "",
        },
    ],
}


def render_plan_prompt(project: dict, task: dict, repo_state: dict | None = None,
                       board: list[dict] | None = None) -> str:
    """Prompt for the PLAN task.

    repo_state: {"empty": bool, "local": bool, "files": [paths...], "readme": str}
    """
    repo_state = repo_state or {}
    repo = project.get("repo") or ""
    base = project.get("base_branch") or "main"
    empty = bool(repo_state.get("empty"))
    files = repo_state.get("files") or []
    if empty:
        repo_desc = ("The repository is EMPTY (only a README with the product prompt). Nothing exists yet, so the "
                     "plan MUST start with a foundation/scaffold task (T1) that every other task depends on.")
    else:
        shown = "\n".join(f"  {f}" for f in files[:80]) + ("\n  …" if len(files) > 80 else "")
        repo_desc = ("The repository already has code. Inspect it before planning (README, package manifests, "
                     "source layout, tests) and plan changes that fit the existing structure and conventions. "
                     "Add a scaffold task only if shared groundwork is genuinely missing.\n"
                     f"Tracked files ({len(files)}):\n{shown or '  (none listed)'}")
    where = (f"GitHub repo `{repo}`, base branch `{base}`" if repo
             else "a local git repository (local mode: no GitHub, every task commits into the same repo)")
    existing = ""
    if board:
        existing = ("\n## Tasks that already exist on the board (do NOT duplicate them)\n"
                    + render_board([b for b in board if b.get("id") != task.get("id")]) + "\n")
    example = json.dumps(PLAN_SCHEMA_EXAMPLE, indent=2)
    return f"""\
You are the PLANNER of an OnlyVibes orchestra: a team of autonomous AI coding agents (Claude Code, Codex,
Vibe, Cursor) running on different laptops. Each agent will get ONE card from your plan, work on its own
git branch in its own clone of the repo, and merge back via a pull request. They cannot see each other's
work-in-progress, only what is already merged plus short chat messages. Your plan is what lets them work
in parallel without breaking each other.

## The product to build
{project.get('prompt', '').strip()}

## Where
You are in {where}. Your working directory is a fresh checkout. {repo_desc}
{existing}
## Your job
Do NOT implement the product. Read what is there, think about the architecture, then write a plan of
4–12 build tasks (fewer for a small product, more only if they are truly independent).

Plan quality rules — these matter more than anything else:
1. **Parallelism.** Maximise tasks that can run at the same time. Dependencies only where a task really
   needs another task's merged code. A typical shape: T1 foundation → several independent feature tasks
   that depend only on T1 → (optionally) one integration/polish task at the end that depends on them.
2. **File ownership.** Every task's description lists the files/directories it OWNS (creates or is the only
   one to edit), e.g. `OWNS: src/timer/*, tests/timer.test.ts`. Two parallel tasks must never own the same
   file. Shared files (package manifest, routing table, app entry, global CSS, README) are owned by the
   foundation task; later tasks should touch them minimally and only by appending.
3. **Explicit interfaces.** When tasks connect, define the contract in BOTH descriptions: exact module paths,
   exported function/class/component names with signatures, data shapes, HTTP routes + JSON payloads, CSS
   class names, CLI commands. E.g. "T3 exports `getStats(range: 'day'|'week'): Stats` from src/stats/api.ts;
   T4 imports it from there." Builders will follow these literally, so be concrete.
4. **Foundation first.** {"Because the repo is empty, T1 is the scaffold: language/framework setup, folder layout, shared types/interfaces stubbed with the exact names other tasks will import, a working test command, a README section on how to run. Every other task depends on T1. Keep T1 small so the others can start quickly." if empty else "If the others need shared groundwork (types, layout, test setup) that doesn't exist yet, put it in T1 and make the others depend on it; otherwise no scaffold is needed."}
5. **Self-contained cards.** Each description is a complete brief for an engineer who has not seen this
   plan: what to build, where (OWNS), which interfaces to provide and to consume, tech choices to follow,
   and what NOT to touch. 4–12 sentences of markdown is a good size.
6. **Acceptance criteria.** 2–5 checkable statements per task (commands that must pass, behaviour that must
   be observable). Every task that writes code also adds/updates tests for it.
7. **Priorities** 1 (urgent, on the critical path) … 4 (nice to have). Foundation = 1.
8. `agent_hint` is normally "". Only set one of {", ".join(KNOWN_AGENTS)} if a task clearly suits it.
9. Pick a sensible, boring, well-known stack unless the prompt says otherwise, and state it in the summary
   and in T1 so everyone uses the same one. No external services needing credentials unless asked.

## Output
Write the plan as JSON exactly in this shape (`ref`s are T1..Tn, `depends_on` lists refs):

```json
{example}
```

Submit it in ONE of these two ways (the first is preferred):
1. Save it to a file outside the repo, e.g. `$OV_STATE_DIR/plan.json`, then run `ov plan $OV_STATE_DIR/plan.json`.
2. Or end your final message with the plan in a single fenced ```json block (nothing after it).

Also add a one-line `title` (2–5 words, the product's name) and a `summary` paragraph explaining the
architecture and why the work is split this way. You may run `ov progress <n> "<note>"` while you work.
Do not commit, push or create files inside the repository.
"""


# ---------------------------------------------------------------- BUILD


def render_build_prompt(project: dict, task: dict, board: list[dict], *, branch: str = "",
                        local_mode: bool = False, agent: str = "", worker_id: str = "") -> str:
    base = project.get("base_branch") or "main"
    key = task.get("key", "T?")
    title = task.get("title", "")
    acceptance = _bullets(task.get("acceptance"), "- (none given — define sensible checks yourself and meet them)")
    summary = (project.get("summary") or "").strip()
    if local_mode:
        git_rules = ("- This is LOCAL MODE: there is no GitHub. Your working directory is a git repo shared with "
                     "other tasks of this project on this laptop. Commit only your own files "
                     "(`git add <paths>`; avoid `git add -A` of other tasks' half-done work). Don't create branches.")
    else:
        git_rules = (f"- You are on branch `{branch}` created from `origin/{base}`. Commit on this branch. "
                     f"Do NOT push, open PRs, merge, rebase or switch branches — the worker does that after you exit.")
    return f"""\
You are one engineer in an OnlyVibes orchestra: several autonomous AI coding agents on different laptops
are building one product together, each on its own task card. You are agent `{agent or 'agent'}` on worker
`{worker_id or '?'}`, assigned to task **{key} · {title}** (task id `{task.get('id', '')}`).

## Product
{project.get('prompt', '').strip()}
{f"{chr(10)}Architecture / plan summary: {summary}{chr(10)}" if summary else ""}
## Your task: {key} · {title}
{(task.get('description') or '(no description)').strip()}

Acceptance criteria — your work is done when all of these hold:
{acceptance}

## The team board (who does what)
{render_board(board, me=task.get('id', ''))}

{_relations(task, board)}

## Talking to your teammates
{OV_HELP}

How to use it — this is part of the job, not optional:
1. Before you start: run `ov inbox` (someone may already have told you about an interface or a change) and
   `ov board`. Then `ov progress 5 "starting: <one-line approach>"`.
2. If you create or change something another task uses (exports, file paths, routes, data shapes, CSS
   classes), announce it once it's committed: `ov say --to <their task id> "<exact interface + where>"`
   (or `ov say` to everyone if several tasks care).
3. If you are blocked on, or unsure about, an interface owned by another task, ask its owner with
   `ov say --to <task id> "..."` and keep going on the parts you can do; don't wait idle. If they don't
   answer, follow the plan's description literally and write down your assumption in your final summary.
4. Run `ov inbox` again between major steps and before finishing, and answer every question addressed to
   you (`ov say --to <their task id> --reply-to <message id> "..."`). Humans may write too — do what they ask
   if it fits your task, and answer them with `ov say --reply-to <message id> "..."` (no `--to`: they read
   the project chat). Never `--to` your own task id. `ov inbox` prints the exact reply command per message.
5. Report progress at real milestones with `ov progress <0-100> "<short note>"` (e.g. 25 scaffolded,
   50 core logic done, 80 tests passing, 100 done).

## Rules
- Work ONLY inside your current working directory (the task's workspace). Never touch files outside it,
  never change global config, never install things system-wide (project-local dependencies are fine).
- Stay in your lane: change only the files/areas your task owns. If you must touch a shared file
  (manifest, router, entry point, README), make a minimal additive edit. Don't refactor or reformat
  other people's code. Don't implement other cards' work — integrate with it instead.
{git_rules}
- Commit early and often with messages starting with your key, e.g. `git commit -m "{key}: add timer state machine"`.
  Leave the working tree clean when you finish (everything committed).
- Run the project's tests/build/linters relevant to your change and make them pass before you finish.
  Add tests for what you build. Never delete or weaken other people's tests to make yours pass.
- Don't ask the human for confirmation: you run fully automatically. Make reasonable decisions and note them.
- Never print or commit secrets/tokens (including $OV_TOKEN).

## Finish
When the acceptance criteria hold and everything is committed: `ov progress 100 "done: <what>"`, check
`ov inbox` one last time, then end with a short summary (3–8 lines): what you built, where (files), the
interfaces other tasks can use, how you tested it, and anything left open.
"""


# ---------------------------------------------------------------- CONFLICTS


def render_conflict_prompt(project: dict, task: dict, files: list[str], *, base: str = "main",
                           branch: str = "") -> str:
    key = task.get("key", "T?")
    return f"""\
You are finishing task **{key} · {task.get('title', '')}** (task id `{task.get('id', '')}`) of the OnlyVibes
project "{project.get('title') or project.get('prompt', '')[:60]}". Your work is committed on branch `{branch}`.
Meanwhile teammates merged their tasks into `{base}`. The worker ran `git merge origin/{base}` in your
current working directory and it stopped with conflicts in:

{_bullets(files)}

Resolve them:
1. Look at each conflicted file (`git status`, `git diff`). The `<<<<<<< HEAD` side is your work, the
   `>>>>>>> origin/{base}` side is what teammates already merged. Keep BOTH intents: their merged code is
   the source of truth for their areas; re-apply your change on top of it. Don't drop their features.
2. Remove every conflict marker, then run the project's tests/build and fix what broke.
3. `git add` the resolved files and commit: `git commit --no-edit -m "{key}: merge {base} and resolve conflicts"`.
4. Do NOT push, rebase, abort the merge, or switch branches — the worker pushes after you exit.
5. If a teammate's interface changed under you, adapt your code to it and tell them with
   `ov say --to <their task id> "..."` if something looks wrong.

End with a 2–4 line summary of how you resolved the conflicts.
"""
