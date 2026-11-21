"""
Mock agent adapter — a fake AI coding agent for demos and tests (no CLI, no LLM).

It behaves like a real agent from the outside:
  * emits thinking / tool_call / tool_result / assistant events over ~20-60 s
    (randomness seeded by the task id, so a given task always plays the same way),
  * talks to the hub through the real `ov` CLI on its PATH: `ov inbox`,
    `ov progress`, `ov say --to <dependent or teammate task>`, and replies to
    messages it receives (also live `chat` frames forwarded by the worker),
  * build tasks: writes a small module + test file and commits them in the workspace,
  * PLAN tasks: derives a 5-7 item plan from the prompt's words, submits it with
    `ov plan` and also ends with a fenced ```json block.

Env vars:
    MOCK_SPEED        speed multiplier (default 1; e.g. 5 = 5x faster, tests use ~50)
    MOCK_SHARED_FILE  optional repo-relative file (e.g. MODULES.md) every build task also
                      appends a line to, so parallel tasks collide and the worker's
                      rebase -> conflict -> agent re-run -> merge path gets exercised
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shlex
import shutil
import tempfile
import time

from adapters import Adapter, AgentResult, Emit, make_event, truncate, RESULT_PREVIEW

STOPWORDS = set("""
a an the and or for with without of to in on at by from into onto that this these those it its is are be
build make create write develop app application web website tool simple small basic nice good some using use
please want need should would could can will let lets me my our your their them they we you i also plus via
about like just very more most new one two three page site system platform based where which who when what
""".split())


def _keywords(prompt: str, n: int = 4) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", prompt.lower())
    seen: list[str] = []
    for w in words:
        if w not in STOPWORDS and w not in seen:
            seen.append(w)
    defaults = ["core", "dashboard", "storage", "settings"]
    return (seen + [d for d in defaults if d not in seen])[:n]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:30] or "module"


def _shared_file(env: dict) -> str:
    """MOCK_SHARED_FILE as a safe repo-relative path ("" when unset or escaping the repo)."""
    raw = (env.get("MOCK_SHARED_FILE") or os.environ.get("MOCK_SHARED_FILE") or "").strip()
    rel = os.path.normpath(raw) if raw else ""
    if not rel or os.path.isabs(rel) or rel.startswith("..") or rel.split(os.sep)[0] == ".git":
        return ""
    return rel


def mock_plan(prompt: str, seed: str) -> dict:
    """A canned but prompt-flavoured 5-7 task plan with sensible depends_on."""
    rng = random.Random(seed)
    kw = _keywords(prompt)
    name = " ".join(w.capitalize() for w in kw[:2]) or "Product"
    a, b, c = kw[0], kw[1], kw[2]
    tasks = [
        {"ref": "T1", "title": "Scaffold: project layout, shared types, test setup",
         "description": f"Create the skeleton for {name}. OWNS: README.md, pyproject/package manifest, src/__init__, "
                        f"src/types.py. INTERFACES: define `{a.capitalize()}` and `{b.capitalize()}` records in src/types.py.",
         "acceptance": ["test command runs", "src/types.py exports the shared records"], "depends_on": [], "priority": 1},
        {"ref": "T2", "title": f"{a.capitalize()} domain model + logic",
         "description": f"OWNS: src/{_slug(a)}/*. Implement the {a} logic on top of src/types.py; export `create_{_slug(a)}()`.",
         "acceptance": [f"unit tests for {a} pass"], "depends_on": ["T1"], "priority": 2},
        {"ref": "T3", "title": f"{b.capitalize()} storage layer",
         "description": f"OWNS: src/storage/*. Persist {b} records; export `save()`/`load()` used by T4 and T5.",
         "acceptance": ["round-trip save/load test passes"], "depends_on": ["T1"], "priority": 2},
        {"ref": "T4", "title": f"{c.capitalize()} API / service endpoints",
         "description": "OWNS: src/api/*. Expose the domain through a small HTTP/CLI interface using T2 and T3.",
         "acceptance": ["endpoints return JSON", "API tests pass"], "depends_on": ["T2", "T3"], "priority": 2},
        {"ref": "T5", "title": f"{name} user interface",
         "description": "OWNS: web/*. Screens that call the API from T4; uses CSS classes `ov-card`, `ov-list`.",
         "acceptance": ["UI renders the main screen", "basic interaction works"], "depends_on": ["T1"], "priority": 3},
    ]
    extras = [
        {"ref": "T6", "title": "Integration tests + CI script",
         "description": "OWNS: tests/integration/*, scripts/ci.sh. End-to-end checks across T2-T5.",
         "acceptance": ["scripts/ci.sh passes"], "depends_on": ["T4", "T5"], "priority": 3},
        {"ref": "T7", "title": "Docs + demo data",
         "description": "OWNS: docs/*, demo/*. How to run, screenshots, seed data.",
         "acceptance": ["docs explain setup in < 5 steps"], "depends_on": ["T1"], "priority": 4},
    ]
    tasks += extras[: rng.randint(0, 2)]
    for t in tasks:
        t["agent_hint"] = ""
    return {"title": name, "summary": f"Mock plan for '{prompt[:80]}': a scaffold first, then {a}, storage, API "
                                      f"and UI in parallel against shared types, integration last.",
            "tasks": tasks}


class MockAdapter(Adapter):
    name = "mock"
    binary = ""
    supports_live_input = True

    def __init__(self) -> None:
        super().__init__()
        self._chats: list[dict] = []

    @classmethod
    def is_available(cls) -> bool:
        return True  # but only enabled when AGENTS lists it (see adapters.detect_agents)

    def build_command(self, prompt: str, cwd: str):
        raise NotImplementedError("mock has no CLI")

    def live_input(self, message: dict) -> bool:
        self._chats.append(message)
        return True

    # ------------------------------------------------------------ helpers

    async def _sh(self, argv: list[str], cwd: str, env: dict, timeout: float = 20) -> tuple[int, str]:
        exe = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, *argv[1:], cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError as e:
            return 127, str(e)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except BaseException as e:  # timeout or cancellation: never leave the child behind
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            if isinstance(e, asyncio.TimeoutError):
                return 124, "timed out"
            raise
        return proc.returncode, out.decode(errors="replace").strip()

    async def _tool(self, emit: Emit, argv: list[str], cwd: str, env: dict) -> tuple[int, str]:
        """Run a shell command the way an agent's Bash tool would, with events."""
        shown = shlex.join(argv)
        emit(make_event("tool_call", f"Bash: {shown}", tool="Bash"))
        rc, out = await self._sh(argv, cwd, env)
        body = out or "(no output)"
        emit(make_event("tool_result", truncate(("" if rc == 0 else f"exit {rc}: ") + body, RESULT_PREVIEW), tool="Bash"))
        return rc, out

    def _tokens(self, rng: random.Random) -> None:
        self.usage = {"input": self.usage["input"] + rng.randint(1500, 6000),
                      "output": self.usage["output"] + rng.randint(80, 600)}

    async def _inbox(self, emit, cwd, env) -> list[dict]:
        rc, out = await self._tool(emit, ["ov", "inbox", "--json"], cwd, env)
        if rc != 0:
            return []
        try:
            return list(json.loads(out).get("messages") or [])
        except ValueError:
            return []

    async def _reply_all(self, emit, cwd, env, messages: list[dict], task: dict, replied: set) -> None:
        """Answer messages addressed to this task/worker and human notes (max 3 per run).

        Broadcast announcements, hub/system notices and replies are not answered,
        so a fleet of mocks doesn't get stuck thanking each other.
        """
        me, worker = task.get("id"), env.get("OV_WORKER_ID")
        for m in messages + self._chats:
            mid = m.get("id")
            frm = m.get("from") or {}
            if not mid or mid in replied or len(replied) >= 3:
                continue
            if frm.get("kind") == "system" or frm.get("task_id") == me or m.get("reply_to"):
                continue
            if frm.get("kind") != "human" and m.get("to") not in (me, worker):
                continue
            replied.add(mid)
            to = frm.get("task_id") or "all"
            who = frm.get("name") or "there"
            text = f"Thanks {who}, noted — {task.get('key')} will follow that. My interface is unchanged."
            await self._tool(emit, ["ov", "say", "--to", to, "--reply-to", mid, text], cwd, env)
        self._chats.clear()

    # ------------------------------------------------------------ run

    async def run(self, prompt: str, cwd: str, env: dict, emit: Emit, ctx: dict | None = None) -> AgentResult:
        self.cwd = cwd
        ctx = ctx or {}
        task = ctx.get("task") or {}
        project = ctx.get("project") or {}
        board = ctx.get("board") or []
        rng = random.Random(task.get("id", "mock"))
        speed = max(0.01, float(env.get("MOCK_SPEED") or os.environ.get("MOCK_SPEED") or 1))
        total = rng.uniform(20, 60) / speed
        kind = task.get("kind") or ("plan" if task.get("key") == "PLAN" else "build")
        steps = 9 if kind == "plan" else 14
        started = time.monotonic()

        async def tick(i: int) -> None:
            # spread the run over `total` seconds, with a little jitter
            target = started + total * (i / steps) * rng.uniform(0.85, 1.1)
            await asyncio.sleep(max(0.0, target - time.monotonic()))
            self._tokens(rng)

        emit(make_event("system", f"Mock agent session started (~{total:.0f}s, seed {task.get('id', '?')})"))
        if ctx.get("mode") == "resolve":
            return await self._run_resolve(cwd, env, emit, task, ctx.get("files") or [], tick)
        if kind == "plan":
            return await self._run_plan(prompt, cwd, env, emit, task, project, rng, tick)
        return await self._run_build(cwd, env, emit, task, project, board, rng, tick)

    async def _run_resolve(self, cwd, env, emit, task, files, tick) -> AgentResult:
        """Conflict re-run: keep both sides of every conflict hunk, then commit."""
        await tick(1)
        emit(make_event("thinking", f"Merge conflicts in {', '.join(files) or '?'}; keeping both sides."))
        await self._tool(emit, ["git", "status", "--short"], cwd, env)
        for rel in files:
            path = os.path.join(cwd, rel)
            try:
                text = open(path, errors="ignore").read()
            except OSError:
                continue
            kept = [ln for ln in text.splitlines(keepends=True)
                    if not re.match(r"^(<{7}|={7}|>{7})( |$)", ln.rstrip("\n"))]
            with open(path, "w") as fh:
                fh.write("".join(kept))
            emit(make_event("tool_call", f"Edit {rel}", tool="Edit"))
            emit(make_event("tool_result", "conflict markers removed, both sides kept", tool="Edit"))
        await tick(5)
        await self._tool(emit, ["git", "add", "-A"], cwd, env)
        key = task.get("key", "T?")
        await self._tool(emit, ["git", "commit", "-q", "--no-edit", "-m", f"{key}: merge and resolve conflicts"],
                         cwd, env)
        final = f"Resolved conflicts in {', '.join(files)} by keeping both sides."
        self.final_text = final
        emit(make_event("assistant", final))
        return AgentResult(0, final, "", dict(self.usage))

    async def _run_plan(self, prompt, cwd, env, emit, task, project, rng, tick) -> AgentResult:
        product = project.get("prompt") or prompt
        await tick(1)
        emit(make_event("thinking", f"The user wants: {truncate(product, 300)}. First I'll look at the repo, "
                                    f"then split the work into parallel cards with clear file ownership."))
        await self._tool(emit, ["ov", "inbox"], cwd, env)
        await tick(2)
        await self._tool(emit, ["git", "ls-files"], cwd, env)
        await tick(3)
        emit(make_event("tool_call", "Read README.md", tool="Read"))
        readme = os.path.join(cwd, "README.md")
        text = open(readme, errors="ignore").read() if os.path.exists(readme) else "(file not found)"
        emit(make_event("tool_result", truncate(text, RESULT_PREVIEW), tool="Read"))
        await tick(4)
        await self._tool(emit, ["ov", "progress", "30", "drafting the task breakdown"], cwd, env)
        plan = mock_plan(product, task.get("id", "mock"))
        await tick(5)
        emit(make_event("thinking", "Breakdown: " + "; ".join(f"{t['ref']} {t['title']}" for t in plan["tasks"])))
        await tick(6)
        state = env.get("OV_STATE_DIR") or tempfile.gettempdir()
        os.makedirs(state, exist_ok=True)
        plan_path = os.path.join(state, "mock-plan.json")
        with open(plan_path, "w") as fh:
            json.dump(plan, fh, indent=2)
        emit(make_event("tool_call", f"Write {plan_path}", tool="Write"))
        emit(make_event("tool_result", f"wrote {len(plan['tasks'])} tasks", tool="Write"))
        await tick(7)
        await self._tool(emit, ["ov", "plan", plan_path], cwd, env)
        await tick(8)
        await self._tool(emit, ["ov", "progress", "100", f"plan ready: {len(plan['tasks'])} tasks"], cwd, env)
        await tick(9)
        final = (f"Planned {len(plan['tasks'])} tasks for {plan['title']}. {plan['summary']}\n\n"
                 f"```json\n{json.dumps(plan, indent=2)}\n```")
        self.final_text = final
        emit(make_event("assistant", final))
        return AgentResult(0, final, "", dict(self.usage))

    async def _run_build(self, cwd, env, emit, task, project, board, rng, tick) -> AgentResult:
        key = task.get("key", "T?")
        title = task.get("title", "task")
        me = task.get("id", "")
        slug = _slug(f"{key}_{title}")
        module = f"src/{slug}.py"
        test = f"tests/test_{slug}.py"
        fn = f"{_slug(title)}_ready"
        dependents = [b for b in board if me in (b.get("depends_on") or [])]
        teammates = [b for b in board if b.get("id") != me and b.get("status") in ("claimed", "in_progress")]
        replied: set[str] = set()

        await tick(1)
        emit(make_event("thinking", f"Task {key}: {title}. Acceptance: "
                                    + "; ".join(task.get("acceptance") or ["(none)"])
                                    + ". I'll check my inbox and the board first."))
        messages = await self._inbox(emit, cwd, env)
        await tick(2)
        await self._tool(emit, ["ov", "board"], cwd, env)
        await self._tool(emit, ["ov", "progress", "5", f"starting: {module} + tests"], cwd, env)
        await tick(3)
        emit(make_event("tool_call", "Read README.md", tool="Read"))
        readme = os.path.join(cwd, "README.md")
        text = open(readme, errors="ignore").read() if os.path.exists(readme) else "(file not found)"
        emit(make_event("tool_result", truncate(text, 600), tool="Read"))
        await tick(4)
        emit(make_event("thinking", f"I own {module}. I'll expose `{fn}()` so dependents can import it."))
        await tick(5)
        os.makedirs(os.path.join(cwd, "src"), exist_ok=True)
        os.makedirs(os.path.join(cwd, "tests"), exist_ok=True)
        with open(os.path.join(cwd, module), "w") as fh:
            fh.write(f'"""{key}: {title} (written by the OnlyVibes mock agent)."""\n\n\n'
                     f"def {fn}() -> bool:\n    return True\n")
        emit(make_event("tool_call", f"Write {module}", tool="Write"))
        emit(make_event("tool_result", "File created successfully", tool="Write"))
        await self._tool(emit, ["ov", "progress", "35", f"{module} written"], cwd, env)
        await tick(6)
        with open(os.path.join(cwd, test), "w") as fh:
            fh.write(f"from src.{slug} import {fn}\n\n\ndef test_{fn}():\n    assert {fn}()\n")
        emit(make_event("tool_call", f"Write {test}", tool="Write"))
        emit(make_event("tool_result", "File created successfully", tool="Write"))
        owned = [module, test]
        shared = _shared_file(env)
        if shared:  # every task appends to the same file: parallel branches will conflict
            os.makedirs(os.path.dirname(os.path.join(cwd, shared)) or cwd, exist_ok=True)
            with open(os.path.join(cwd, shared), "a") as fh:
                fh.write(f"- {key}: `{fn}()` in {module}\n")
            emit(make_event("tool_call", f"Edit {shared}", tool="Edit"))
            emit(make_event("tool_result", f"listed {module} in {shared}", tool="Edit"))
            owned.append(shared)
        await tick(7)
        target = dependents[0] if dependents else (teammates[0] if teammates else None)
        if target:
            await self._tool(emit, ["ov", "say", "--to", target["id"],
                                    f"{key} here: `{fn}()` is exported from {module} — import it from there."],
                             cwd, env)
        else:
            await self._tool(emit, ["ov", "say", f"{key} here: `{fn}()` lives in {module}."], cwd, env)
        await tick(8)
        emit(make_event("tool_call", "Bash: python -m pytest -q", tool="Bash"))
        emit(make_event("tool_result", f"1 passed in 0.0{rng.randint(1, 9)}s", tool="Bash"))
        await self._tool(emit, ["ov", "progress", "70", "tests passing"], cwd, env)
        await tick(9)
        rc = 1
        for attempt in range(4):  # local mode shares one repo: the index lock may be busy
            await self._tool(emit, ["git", "add", "--", *owned], cwd, env)
            rc, _ = await self._tool(emit, ["git", "commit", "-q", "-m", f"{key}: {title}", "--", *owned],
                                     cwd, env)
            if rc == 0:
                break
            await asyncio.sleep(0.3 + attempt * 0.5)
        await tick(10)
        messages += await self._inbox(emit, cwd, env)
        await self._reply_all(emit, cwd, env, messages, task, replied)
        await tick(11)
        emit(make_event("thinking", "All acceptance criteria covered; wrapping up."))
        await tick(12)
        await self._reply_all(emit, cwd, env, await self._inbox(emit, cwd, env), task, replied)
        await self._tool(emit, ["ov", "progress", "100", f"done: {module}"], cwd, env)
        await tick(14)
        final = (f"{key} done: added {module} exporting `{fn}()` and {test}. "
                 f"{'Committed.' if rc == 0 else 'Commit failed (left for the worker).'} "
                 f"Told {target['key'] if target else 'the team'} about the interface; "
                 f"answered {len(replied)} message(s).")
        self.final_text = final
        emit(make_event("assistant", final))
        return AgentResult(0, final, "", dict(self.usage))
