"""
Agent adapters — turn one AI coding agent CLI into a stream of transcript events.

Every adapter knows how to (1) tell whether its CLI is installed, (2) build the
argv for a fully automatic headless run inside a task workspace, and (3) parse
each stdout line into PROTOCOL.md §1 events
`{"ts", "kind", "text", "tool"?, "data"?}` while tracking token usage and the
final answer. `Adapter.run()` spawns the CLI in its own process group so the
whole tree can be killed on cancel / shutdown.

    name          command (cwd = task workspace, never shell=True)
    claude-code   claude -p --output-format stream-json --verbose --dangerously-skip-permissions   (prompt on stdin)
    codex         codex exec --json --sandbox danger-full-access --skip-git-repo-check -            (prompt on stdin)
    vibe          vibe --prompt <prompt> --auto-approve [--output streaming]                         (flags probed from --help)
    cursor        cursor-agent -p --output-format stream-json --force <prompt>
    mock          no CLI, simulated agent (see mock_agent.py)

Formats of claude-code and codex were verified against the installed CLIs
(fixtures in tests/fixtures/). vibe and cursor are implemented from their
documented stream formats and degrade gracefully: any line that is not
understood becomes an `assistant` event.

Env vars:
    AGENTS   comma list of adapter names: overrides auto-detection and sets the
             preference order (the only way to enable `mock`)
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

MAX_TEXT = 4000           # protocol limit for event.text
MAX_DATA = 8 * 1024       # protocol limit for event.data (serialized)
RESULT_PREVIEW = 1200     # tool results are truncated harder: they are noisy
MAX_ARGV_PROMPT = 100_000  # beyond this, argv prompts go through a file
STDERR_EVENTS = 200       # max stderr lines forwarded as events per run

Emit = Callable[[dict], None]


# ---------------------------------------------------------------- helpers


def truncate(text: str, limit: int = MAX_TEXT) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def make_event(kind: str, text: str, tool: str | None = None, data: dict | None = None) -> dict:
    """A §1 transcript event without `seq` (assigned by the server)."""
    ev: dict = {"ts": time.time(), "kind": kind, "text": truncate(text)}
    if tool:
        ev["tool"] = tool
    if data:
        try:
            blob = json.dumps(data, default=str)
        except (TypeError, ValueError):
            blob = ""
        if blob and len(blob) <= MAX_DATA:
            ev["data"] = data
    return ev


def _one_line(text: str, limit: int = 300) -> str:
    return truncate(" ".join(str(text).split()), limit)


def _rel(path: str, cwd: str) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1:]
    return path


def _first(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return ""


_TOOL_ALIASES = {
    "bash": "Bash", "shell": "Bash", "run_shell_command": "Bash", "exec": "Bash", "command": "Bash",
    "terminal": "Bash", "run_terminal_cmd": "Bash", "local_shell": "Bash",
    "read": "Read", "read_file": "Read", "view": "Read", "cat": "Read",
    "edit": "Edit", "multiedit": "Edit", "search_replace": "Edit", "str_replace": "Edit",
    "str_replace_editor": "Edit", "edit_file": "Edit", "apply_patch": "Edit", "notebookedit": "Edit",
    "write": "Write", "write_file": "Write", "create_file": "Write",
    "glob": "Glob", "find": "Glob", "file_search": "Glob",
    "grep": "Grep", "search": "Grep", "codebase_search": "Grep",
    "ls": "LS", "list_dir": "LS", "list_directory": "LS",
    "delete": "Delete", "delete_file": "Delete",
    "webfetch": "WebFetch", "fetch": "WebFetch", "web_fetch": "WebFetch",
    "websearch": "WebSearch", "web_search": "WebSearch",
    "todowrite": "Todo", "todo": "Todo", "update_plan": "Todo",
    "task": "Task", "agent": "Task",
}


def describe_tool(name: str, args, cwd: str = "") -> tuple[str, str]:
    """Readable one-liner for a tool call: ("Bash", "Bash: npm test")."""
    args = args if isinstance(args, dict) else {}
    canon = _TOOL_ALIASES.get(str(name).lower().replace("-", "_"), name or "Tool")
    path = _rel(_first(args, "file_path", "path", "filePath", "target_file", "notebook_path", "file"), cwd)
    if canon == "Bash":
        cmd = args.get("command") or args.get("cmd") or ""
        if isinstance(cmd, list):
            cmd = shlex.join(str(c) for c in cmd)
        return canon, f"Bash: {_one_line(cmd, 500)}"
    if canon in ("Read", "Edit", "Write", "Delete"):
        return canon, f"{canon} {path}".rstrip()
    if canon in ("Glob", "Grep"):
        pat = _first(args, "pattern", "query", "glob", "regex", "glob_pattern")
        where = f" in {path}" if path else ""
        return canon, f"{canon} {_one_line(pat, 200)}{where}"
    if canon == "LS":
        return canon, f"LS {path or '.'}"
    if canon == "WebFetch":
        return canon, f"WebFetch {_first(args, 'url')}"
    if canon == "WebSearch":
        return canon, f"WebSearch {_one_line(_first(args, 'query', 'q'), 200)}"
    if canon == "Todo":
        todos = args.get("todos") or args.get("plan") or args.get("items") or []
        if isinstance(todos, list) and todos:
            active = next((t for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"), None)
            label = _first(active or {}, "content", "step", "activeForm", "text")
            done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
            return canon, f"Todo: {done}/{len(todos)} done" + (f" · now: {_one_line(label, 120)}" if label else "")
        return canon, "Todo: update"
    if canon == "Task":
        return canon, f"Task: {_one_line(_first(args, 'description', 'prompt'), 200)}"
    compact = _one_line(json.dumps(args, default=str), 200) if args else ""
    return str(canon), f"{canon} {compact}".rstrip()


def _content_text(content) -> str:
    """Flatten tool_result content (str | list of blocks) into text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(str(b.get("text", "")))
                elif b.get("type") == "image":
                    parts.append("[image]")
            else:
                parts.append(str(b))
        return "\n".join(parts)
    if isinstance(content, dict):
        return _first(content, "text", "output", "stdout", "content", "message", "error") or json.dumps(content, default=str)
    return str(content)


def _json(line: str):
    s = line.strip()
    if not s.startswith("{"):
        return None
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


# ---------------------------------------------------------------- base


@dataclass
class Command:
    argv: list[str]
    stdin: str | None = None          # prompt piped on stdin (None = stdin is /dev/null)
    cleanup: list[str] = field(default_factory=list)  # temp files to delete afterwards


@dataclass
class AgentResult:
    exit_code: int
    final_text: str
    error: str = ""
    usage: dict = field(default_factory=lambda: {"input": 0, "output": 0})

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.error


class Adapter:
    """Base adapter: subclass and implement build_command + parse_line."""

    name = ""
    binary = ""
    supports_live_input = False

    def __init__(self) -> None:
        self.usage = {"input": 0, "output": 0}
        self.final_text = ""
        self.assistant_texts: list[str] = []
        self.error_text = ""
        self.cwd = ""
        self.proc: asyncio.subprocess.Process | None = None

    # -- detection / command

    @classmethod
    def is_available(cls) -> bool:
        return bool(cls.binary and shutil.which(cls.binary))

    def build_command(self, prompt: str, cwd: str) -> Command:
        raise NotImplementedError

    def _argv_prompt(self, prompt: str) -> tuple[str, list[str]]:
        """Prompt as one argv element; very long prompts go through a temp file."""
        if len(prompt.encode()) <= MAX_ARGV_PROMPT:
            return prompt, []
        fd, path = tempfile.mkstemp(prefix="ov-prompt-", suffix=".md")
        with os.fdopen(fd, "w") as fh:
            fh.write(prompt)
        return (f"Your full instructions are in the file {path}. Read that whole file first "
                f"and follow it exactly."), [path]

    # -- parsing

    def parse_line(self, line: str) -> list[dict]:
        return self._text_line(line)

    def finish(self) -> list[dict]:
        """Called once after stdout closed: flush any buffered state."""
        return []

    def _text_line(self, line: str) -> list[dict]:
        text = line.rstrip("\n")
        if not text.strip():
            return []
        self.assistant_texts.append(text)
        return [make_event("assistant", text)]

    def _assistant(self, text: str) -> list[dict]:
        text = (text or "").strip()
        if not text:
            return []
        self.assistant_texts.append(text)
        return [make_event("assistant", text)]

    def last_text(self) -> str:
        return self.final_text or (self.assistant_texts[-1] if self.assistant_texts else "")

    def live_input(self, message: dict) -> bool:
        """Deliver a chat message to the running agent; False if unsupported."""
        return False

    # -- running

    async def run(self, prompt: str, cwd: str, env: dict, emit: Emit, ctx: dict | None = None) -> AgentResult:
        """Spawn the CLI, stream events through `emit`, return the outcome.

        Cancelling the awaiting asyncio task kills the agent's process group.
        """
        self.cwd = cwd
        cmd = self.build_command(prompt, cwd)
        proc = await asyncio.create_subprocess_exec(
            *cmd.argv, cwd=cwd, env=env,
            stdin=asyncio.subprocess.PIPE if cmd.stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=64 * 1024 * 1024,
        )
        self.proc = proc
        stderr_tail: deque[str] = deque(maxlen=20)

        async def feed_stdin() -> None:
            try:
                proc.stdin.write(cmd.stdin.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        async def read_stderr() -> None:
            forwarded = 0
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                stderr_tail.append(line)
                if forwarded < STDERR_EVENTS:
                    forwarded += 1
                    emit(make_event("system", f"stderr: {line}"))

        helpers = [asyncio.create_task(read_stderr())]
        if cmd.stdin is not None:
            helpers.append(asyncio.create_task(feed_stdin()))
        try:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace")
                try:
                    events = self.parse_line(line)
                except Exception as e:  # a parser bug must never kill the run
                    events = [make_event("system", f"(unparsed line: {e}) {line.strip()}")]
                for ev in events:
                    emit(ev)
            for ev in self.finish():
                emit(ev)
            await asyncio.gather(*helpers, return_exceptions=True)
            rc = await proc.wait()
        finally:
            if proc.returncode is None:
                await kill_process_group(proc)
            for h in helpers:
                h.cancel()
            for path in cmd.cleanup:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        error = ""
        if rc != 0:
            tail = " | ".join(list(stderr_tail)[-5:])
            error = self.error_text or f"{self.name} exited with code {rc}" + (f": {tail}" if tail else "")
        elif self.error_text:
            error = self.error_text
        return AgentResult(rc, self.last_text(), truncate(error, 2000), dict(self.usage))


async def kill_process_group(proc: asyncio.subprocess.Process, grace: float = 3.0) -> None:
    """SIGTERM the whole process group, then SIGKILL after `grace` seconds."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=grace)
            # the leader is gone; make sure no grandchildren survive either
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            return
        except asyncio.TimeoutError:
            continue


# ---------------------------------------------------------------- claude-code


class ClaudeCodeAdapter(Adapter):
    """`claude -p` with stream-json output (one JSON object per line)."""

    name = "claude-code"
    binary = "claude"

    def __init__(self) -> None:
        super().__init__()
        self._msg_usage: dict[str, dict] = {}
        self._tool_names: dict[str, str] = {}

    def build_command(self, prompt: str, cwd: str) -> Command:
        # Prompt on stdin: no argv size limit, no risk of being parsed as a flag.
        return Command([self.binary, "-p", "--output-format", "stream-json", "--verbose",
                        "--dangerously-skip-permissions"], stdin=prompt)

    @staticmethod
    def _tokens(u: dict) -> tuple[int, int]:
        inp = sum(int(u.get(k) or 0) for k in
                  ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        return inp, int(u.get("output_tokens") or 0)

    def parse_line(self, line: str) -> list[dict]:
        obj = _json(line)
        if obj is None:
            return self._text_line(line)
        t = obj.get("type")
        sub = " [subagent]" if obj.get("parent_tool_use_id") else ""
        if t == "system":
            if obj.get("subtype") == "init":
                return [make_event("system", f"Claude Code session started · model {obj.get('model', '?')} · "
                                             f"{obj.get('permissionMode', '')}".rstrip(" ·"),
                                   data={"session_id": obj.get("session_id")})]
            return []
        if t == "assistant":
            msg = obj.get("message") or {}
            if isinstance(msg.get("usage"), dict):
                self._msg_usage[msg.get("id") or str(len(self._msg_usage))] = msg["usage"]
                tot = [self._tokens(u) for u in self._msg_usage.values()]
                self.usage = {"input": sum(a for a, _ in tot), "output": sum(b for _, b in tot)}
            events = []
            for block in msg.get("content") or []:
                bt = block.get("type")
                if bt == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        if not sub:
                            self.assistant_texts.append(text)
                        events.append(make_event("assistant", text if not sub else f"{sub.strip()} {text}"))
                elif bt == "thinking":
                    if (block.get("thinking") or "").strip():
                        events.append(make_event("thinking", block["thinking"].strip()))
                elif bt == "tool_use":
                    name = block.get("name", "Tool")
                    self._tool_names[block.get("id", "")] = name
                    tool, text = describe_tool(name, block.get("input"), self.cwd)
                    events.append(make_event("tool_call", text + sub, tool=tool,
                                             data={"id": block.get("id"), "input": block.get("input")}))
            return events
        if t == "user":
            content = (obj.get("message") or {}).get("content")
            events = []
            if isinstance(content, list):
                for block in content:
                    if block.get("type") != "tool_result":
                        continue
                    name = self._tool_names.get(block.get("tool_use_id", ""), "")
                    tool = _TOOL_ALIASES.get(name.lower(), name)
                    body = _content_text(block.get("content")).strip() or "(no output)"
                    prefix = "error: " if block.get("is_error") else ""
                    events.append(make_event("tool_result", prefix + truncate(body, RESULT_PREVIEW),
                                             tool=tool or None,
                                             data={"id": block.get("tool_use_id"), "is_error": bool(block.get("is_error"))}))
            return events
        if t == "result":
            if isinstance(obj.get("usage"), dict):
                inp, out = self._tokens(obj["usage"])
                self.usage = {"input": inp, "output": out}
            self.final_text = str(obj.get("result") or "")
            secs = (obj.get("duration_ms") or 0) / 1000
            cost = obj.get("total_cost_usd")
            summary = (f"Finished ({obj.get('subtype', 'done')}) · {obj.get('num_turns', '?')} turns · {secs:.1f}s"
                       + (f" · ${cost:.2f}" if isinstance(cost, (int, float)) else ""))
            events = [make_event("system", summary)]
            if obj.get("is_error"):
                self.error_text = truncate(self.final_text or obj.get("subtype") or "claude reported an error", 2000)
                events.append(make_event("error", self.error_text))
            return events
        return []  # rate_limit_event, stream_event, hooks, …


# ---------------------------------------------------------------- codex


class CodexAdapter(Adapter):
    """`codex exec --json`: thread/turn/item JSONL events."""

    name = "codex"
    binary = "codex"

    def build_command(self, prompt: str, cwd: str) -> Command:
        # "-" = read the instructions from stdin (see `codex exec --help`).
        return Command([self.binary, "exec", "--json", "--sandbox", "danger-full-access",
                        "--skip-git-repo-check", "--color", "never", "-"], stdin=prompt)

    @staticmethod
    def _unwrap(command) -> str:
        if isinstance(command, list):
            parts = [str(c) for c in command]
        else:
            try:
                parts = shlex.split(str(command))
            except ValueError:
                return str(command)
        if len(parts) == 3 and parts[0].endswith(("bash", "sh", "zsh")) and parts[1] in ("-lc", "-c"):
            return parts[2]
        return shlex.join(parts)

    def parse_line(self, line: str) -> list[dict]:
        obj = _json(line)
        if obj is None:
            return self._text_line(line)
        t = obj.get("type", "")
        item = obj.get("item") or {}
        it = item.get("type", "")
        if t == "thread.started":
            return [make_event("system", "Codex session started", data={"thread_id": obj.get("thread_id")})]
        if t == "turn.completed":
            u = obj.get("usage") or {}
            self.usage = {"input": self.usage["input"] + int(u.get("input_tokens") or 0),
                          "output": self.usage["output"] + int(u.get("output_tokens") or 0)}
            return []
        if t == "turn.failed":
            err = (obj.get("error") or {}).get("message") or "turn failed"
            self.error_text = truncate(err, 2000)
            return [make_event("error", err)]
        if t == "error":
            return [make_event("error", obj.get("message") or json.dumps(obj))]
        if t == "item.started":
            if it == "command_execution":
                cmd = self._unwrap(item.get("command", ""))
                return [make_event("tool_call", f"Bash: {_one_line(cmd, 500)}", tool="Bash",
                                   data={"id": item.get("id"), "command": cmd})]
            if it == "mcp_tool_call":
                name = f"{item.get('server', 'mcp')}.{item.get('tool', '?')}"
                return [make_event("tool_call", f"{name} {_one_line(json.dumps(item.get('arguments') or {}), 200)}",
                                   tool=name, data={"id": item.get("id")})]
            return []
        if t not in ("item.completed", "item.updated"):
            return []
        if it == "agent_message":
            text = (item.get("text") or "").strip()
            if t == "item.completed" and text:
                self.final_text = text
                return self._assistant(text)
            return []
        if it == "reasoning":
            text = (item.get("text") or "").strip()
            return [make_event("thinking", text)] if text and t == "item.completed" else []
        if it == "command_execution" and t == "item.completed":
            out = (item.get("aggregated_output") or "").strip() or "(no output)"
            code = item.get("exit_code")
            prefix = f"exit {code}: " if code not in (0, None) else ""
            return [make_event("tool_result", prefix + truncate(out, RESULT_PREVIEW), tool="Bash",
                               data={"id": item.get("id"), "exit_code": code})]
        if it == "file_change" and t == "item.completed":
            verbs = {"add": "Write", "delete": "Delete", "update": "Edit"}
            changes = item.get("changes") or []
            text = ", ".join(f"{verbs.get(c.get('kind'), 'Edit')} {_rel(str(c.get('path', '')), self.cwd)}"
                             for c in changes if isinstance(c, dict)) or "Edit files"
            return [make_event("tool_call", text, tool="Edit", data={"id": item.get("id")})]
        if it == "mcp_tool_call" and t == "item.completed":
            res = item.get("result") or item.get("error") or ""
            return [make_event("tool_result", truncate(_content_text(res) or item.get("status", ""), RESULT_PREVIEW),
                               tool=f"{item.get('server', 'mcp')}.{item.get('tool', '?')}")]
        if it == "web_search" and t == "item.completed":
            return [make_event("tool_call", f"WebSearch {_one_line(item.get('query', ''), 200)}", tool="WebSearch")]
        if it == "todo_list":
            todos = item.get("items") or []
            lines = [("[x] " if x.get("completed") else "[ ] ") + str(x.get("text", "")) for x in todos if isinstance(x, dict)]
            return [make_event("thinking", "Plan:\n" + "\n".join(lines))] if lines else []
        if it == "error":
            return [make_event("error", item.get("message") or "error")]
        return []


# ---------------------------------------------------------------- cursor


class CursorAdapter(Adapter):
    """`cursor-agent -p --output-format stream-json`.

    Known line types: system/init, user, assistant (message.content[] text),
    thinking (delta/completed), tool_call (started/completed with a
    `<kind>ToolCall` payload), result. Anything else is shown as text.
    """

    name = "cursor"
    binary = "cursor-agent"

    def __init__(self) -> None:
        super().__init__()
        self._thinking: list[str] = []

    def build_command(self, prompt: str, cwd: str) -> Command:
        text, cleanup = self._argv_prompt(prompt)
        return Command([self.binary, "-p", "--output-format", "stream-json", "--force", text], cleanup=cleanup)

    def _flush_thinking(self) -> list[dict]:
        text = "".join(self._thinking).strip()
        self._thinking = []
        return [make_event("thinking", text)] if text else []

    def finish(self) -> list[dict]:
        return self._flush_thinking()

    @staticmethod
    def _tool_payload(tc: dict) -> tuple[str, dict, dict]:
        """({kind}ToolCall: {args, result}) -> (name, args, result)."""
        for key, val in tc.items():
            if not isinstance(val, dict):
                continue
            if key == "function":
                args = val.get("arguments")
                try:
                    args = json.loads(args) if isinstance(args, str) else (args or {})
                except ValueError:
                    args = {"arguments": args}
                return str(val.get("name", "tool")), args, val.get("result") or {}
            name = key[:-len("ToolCall")] if key.endswith("ToolCall") else key
            return name, val.get("args") or {}, val.get("result") or {}
        return "tool", {}, {}

    def parse_line(self, line: str) -> list[dict]:
        obj = _json(line)
        if obj is None:
            return self._flush_thinking() + self._text_line(line)
        t, sub = obj.get("type"), obj.get("subtype")
        if t == "thinking":
            if sub == "delta":
                self._thinking.append(str(obj.get("text", "")))
                return []
            if obj.get("text"):
                self._thinking.append(str(obj["text"]))
            return self._flush_thinking()
        events = self._flush_thinking()
        if t == "system":
            if sub == "init":
                events.append(make_event("system", f"Cursor agent started · model {obj.get('model', '?')}"))
            return events
        if t == "user":
            return events
        if t == "assistant":
            content = (obj.get("message") or {}).get("content")
            return events + self._assistant(_content_text(content))
        if t == "tool_call":
            name, args, result = self._tool_payload(obj.get("tool_call") or {})
            name = {"shell": "Bash", "read": "Read", "edit": "Edit", "write": "Write", "grep": "Grep",
                    "glob": "Glob", "ls": "LS", "delete": "Delete", "todo": "Todo", "updateTodos": "Todo"}.get(name, name)
            if sub == "started":
                tool, text = describe_tool(name, args, self.cwd)
                return events + [make_event("tool_call", text, tool=tool, data={"id": obj.get("call_id")})]
            if sub == "completed":
                if isinstance(result, dict) and "error" in result:
                    body = "error: " + _content_text(result["error"])
                else:
                    ok = result.get("success", result) if isinstance(result, dict) else result
                    body = _content_text(ok) if not isinstance(ok, dict) else (
                        _first(ok, "stdout", "content", "output", "message")
                        or (f"exit {ok['exitCode']}" if "exitCode" in ok else "done"))
                return events + [make_event("tool_result", truncate(body.strip() or "done", RESULT_PREVIEW),
                                            tool=_TOOL_ALIASES.get(name.lower(), name))]
            return events
        if t == "result":
            self.final_text = str(obj.get("result") or "")
            u = obj.get("usage") or {}
            if u:
                self.usage = {"input": int(u.get("inputTokens") or u.get("input_tokens") or 0),
                              "output": int(u.get("outputTokens") or u.get("output_tokens") or 0)}
            if obj.get("is_error") or sub == "error":
                self.error_text = truncate(self.final_text or "cursor-agent reported an error", 2000)
                return events + [make_event("error", self.error_text)]
            secs = (obj.get("duration_ms") or 0) / 1000
            return events + [make_event("system", f"Finished ({sub or 'done'}) · {secs:.1f}s")]
        return events + self._text_line(line)


# ---------------------------------------------------------------- vibe


class VibeAdapter(Adapter):
    """Mistral Vibe CLI in programmatic mode.

    `vibe --prompt <p> --auto-approve`; when `vibe --help` lists `--output`,
    `--output streaming` is added and the newline-delimited message objects
    (`{"role": "assistant"|"tool", "content", "tool_calls"}`) are parsed.
    Plain text lines become assistant events.
    """

    name = "vibe"
    binary = "vibe"
    _help_cache: str | None = None

    @classmethod
    def _help(cls) -> str:
        if cls._help_cache is None:
            try:
                out = subprocess.run([cls.binary, "--help"], capture_output=True, text=True, timeout=10,
                                     stdin=subprocess.DEVNULL)
                cls._help_cache = out.stdout + out.stderr
            except Exception:
                cls._help_cache = ""
        return cls._help_cache

    def build_command(self, prompt: str, cwd: str) -> Command:
        text, cleanup = self._argv_prompt(prompt)
        help_text = self._help()
        argv = [self.binary, "--prompt", text]
        if "--auto-approve" in help_text or not help_text:
            argv.append("--auto-approve")
        if "--output" in help_text and "streaming" in help_text:
            argv += ["--output", "streaming"]
        return Command(argv, cleanup=cleanup)

    def parse_line(self, line: str) -> list[dict]:
        obj = _json(line)
        if obj is None or "role" not in obj:
            if obj is not None and obj.get("type") == "usage":
                self.usage = {"input": int(obj.get("prompt_tokens") or 0), "output": int(obj.get("completion_tokens") or 0)}
                return []
            return self._text_line(line)
        role = obj.get("role")
        events: list[dict] = []
        if role == "assistant":
            if obj.get("reasoning_content"):
                events.append(make_event("thinking", str(obj["reasoning_content"])))
            events += self._assistant(_content_text(obj.get("content")))
            if events and events[-1]["kind"] == "assistant":
                self.final_text = events[-1]["text"]
            for call in obj.get("tool_calls") or []:
                fn = call.get("function") or {}
                args = fn.get("arguments")
                try:
                    args = json.loads(args) if isinstance(args, str) else (args or {})
                except ValueError:
                    args = {"arguments": args}
                tool, text = describe_tool(fn.get("name", "tool"), args, self.cwd)
                events.append(make_event("tool_call", text, tool=tool, data={"id": call.get("id")}))
        elif role == "tool":
            body = _content_text(obj.get("content")).strip() or "(no output)"
            name = obj.get("name") or ""
            events.append(make_event("tool_result", truncate(body, RESULT_PREVIEW),
                                     tool=_TOOL_ALIASES.get(str(name).lower(), name) or None))
        u = obj.get("usage")
        if isinstance(u, dict):
            self.usage = {"input": int(u.get("prompt_tokens") or u.get("input_tokens") or 0),
                          "output": int(u.get("completion_tokens") or u.get("output_tokens") or 0)}
        return events  # system / user echoes are skipped


# ---------------------------------------------------------------- registry


def _mock_cls():
    from mock_agent import MockAdapter  # local import: keeps the mock out of the CLI path
    return MockAdapter


ADAPTERS: dict[str, type[Adapter] | Callable[[], type[Adapter]]] = {
    "claude-code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "vibe": VibeAdapter,
    "cursor": CursorAdapter,
    "mock": _mock_cls,
}
DETECT_ORDER = ["claude-code", "codex", "cursor", "vibe"]


def adapter_class(name: str) -> type[Adapter]:
    entry = ADAPTERS[name]
    return entry if isinstance(entry, type) else entry()


def make_adapter(name: str) -> Adapter:
    return adapter_class(name)()


def detect_agents(agents_env: str | None = None) -> list[str]:
    """Adapters usable on this laptop, in preference order.

    With AGENTS set, it is the list (unknown names dropped; installed-ness is
    still checked for the real CLIs so a typo can't make every task fail).
    """
    agents_env = os.environ.get("AGENTS", "") if agents_env is None else agents_env
    wanted = [a.strip() for a in agents_env.split(",") if a.strip()]
    if wanted:
        out = []
        for name in wanted:
            if name not in ADAPTERS or name in out:
                continue
            if name == "mock" or adapter_class(name).is_available():
                out.append(name)
        return out
    return [n for n in DETECT_ORDER if adapter_class(n).is_available()]
