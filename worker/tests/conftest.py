"""Shared test fixtures: isolated git config, a fake `gh`, bare "origin" repos,
free localhost ports in 8765-8799, and a stub of the hub's agent HTTP API."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

WORKER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WORKER_DIR)
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


# ---------------------------------------------------------------- git isolation


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    """Never touch the developer's git config (signing keys, identity, hooks)."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text(textwrap.dedent("""\
        [user]
            name = Test Worker
            email = test@example.invalid
        [init]
            defaultBranch = main
        [commit]
            gpgsign = false
        [tag]
            gpgsign = false
        [advice]
            detachedHead = false
        """))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_DIR"):
        monkeypatch.delenv(var, raising=False)
    yield


def git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


# ---------------------------------------------------------------- fake gh + origin


FAKE_GH = r'''#!{python}
"""Fake `gh`: records every call; repo clone / pr create|merge|view against local bare repos."""
import json, os, subprocess, sys, tempfile

root = os.environ["FAKE_GH_ROOT"]
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as fh:
    fh.write(json.dumps({{"args": args, "cwd": os.getcwd()}}) + "\n")
state = os.path.join(root, "prs.json")

def load():
    return json.load(open(state)) if os.path.exists(state) else []

def save(prs):
    json.dump(prs, open(state, "w"))

def opt(name):
    return args[args.index(name) + 1] if name in args else None

def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout

def find(target):
    for p in load():
        if p["url"] == target or p["head"] == target:
            return p
    return None

if args[:2] == ["repo", "clone"]:
    sys.exit(subprocess.run(["git", "clone", "-q", os.path.join(root, args[2] + ".git"), args[3]]).returncode)

if args[:2] == ["pr", "create"]:
    prs = load()
    head, base = opt("--head"), opt("--base")
    url_origin = git("remote", "get-url", "origin", cwd=os.getcwd()).strip()
    repo = os.path.relpath(url_origin, root)[:-4]
    for p in prs:
        if p["head"] == head and p["state"] == "OPEN":
            print(f'a pull request for branch "{{head}}" into branch "{{base}}" already exists:\n{{p["url"]}}', file=sys.stderr)
            sys.exit(1)
    url = f"https://github.com/{{repo}}/pull/{{len(prs) + 1}}"
    prs.append({{"url": url, "repo": repo, "head": head, "base": base, "title": opt("--title"),
                "body": opt("--body"), "state": "OPEN"}})
    save(prs)
    print(url)
    sys.exit(0)

if args[:2] == ["pr", "merge"]:
    counter = os.path.join(root, "merge_failures")
    left = int(open(counter).read()) if os.path.exists(counter) else int(os.environ.get("FAKE_GH_MERGE_FAILS", "0"))
    if left > 0:
        open(counter, "w").write(str(left - 1))
        print("GraphQL: Pull request is not mergeable", file=sys.stderr)
        sys.exit(1)
    open(counter, "w").write("0")
    prs = load()
    pr = next(p for p in prs if p["url"] == args[2])
    tmp = tempfile.mkdtemp()
    git("clone", "-q", os.path.join(root, pr["repo"] + ".git"), tmp)
    git("checkout", "-q", pr["base"], cwd=tmp)
    r = subprocess.run(["git", "merge", "--squash", "origin/" + pr["head"]], cwd=tmp, capture_output=True, text=True)
    if r.returncode:
        print("merge conflict", file=sys.stderr)
        sys.exit(1)
    git("commit", "-q", "-m", pr["title"], cwd=tmp)
    git("push", "-q", "origin", pr["base"], cwd=tmp)
    if "--delete-branch" in args:
        git("push", "-q", "origin", "--delete", pr["head"], cwd=tmp)
    pr["state"] = "MERGED"
    save(prs)
    sys.exit(0)

if args[:2] == ["pr", "view"]:
    pr = find(args[2])
    if not pr:
        print("no pull requests found", file=sys.stderr)
        sys.exit(1)
    q = opt("-q")
    print(pr[q.lstrip(".")] if q else json.dumps(pr))
    sys.exit(0)

print("fake gh: unsupported " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


class FakeGH:
    def __init__(self, root: str):
        self.root = root
        self.log = os.path.join(root, "gh_calls.jsonl")
        self.bin = os.path.join(root, "gh")
        with open(self.bin, "w") as fh:
            fh.write(FAKE_GH.format(python=sys.executable))
        os.chmod(self.bin, 0o755)

    def calls(self) -> list[list[str]]:
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [json.loads(line)["args"] for line in fh if line.strip()]

    def bare(self, repo: str, seed: bool = True) -> str:
        """Create <root>/<repo>.git; with seed, main has one commit with README.md."""
        path = os.path.join(self.root, repo + ".git")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        git("init", "-q", "--bare", "-b", "main", path)
        if seed:
            work = os.path.join(self.root, "_seed_" + repo.replace("/", "_"))
            git("clone", "-q", path, work)
            with open(os.path.join(work, "README.md"), "w") as fh:
                fh.write("# App\n\nline one\n")
            git("add", "README.md", cwd=work)
            git("commit", "-q", "-m", "init", cwd=work)
            git("push", "-q", "origin", "main", cwd=work)
        return path

    def show(self, repo: str, ref: str, path: str) -> str:
        return git("--git-dir", os.path.join(self.root, repo + ".git"), "show", f"{ref}:{path}")


@pytest.fixture
def fake_gh(tmp_path, monkeypatch) -> FakeGH:
    root = tmp_path / "gh"
    root.mkdir()
    gh = FakeGH(str(root))
    monkeypatch.setenv("GH_BIN", gh.bin)
    monkeypatch.setenv("FAKE_GH_ROOT", gh.root)
    monkeypatch.setenv("FAKE_GH_LOG", gh.log)
    return gh


# ---------------------------------------------------------------- ports


def free_port() -> int:
    """A free localhost port in the 8765-8799 range reserved for tests."""
    for port in range(8765, 8800):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 8765-8799")


# ---------------------------------------------------------------- stub agent API


class StubHub:
    """Tiny threaded HTTP server implementing /api/agent/* well enough for ov + mock."""

    def __init__(self, token: str = "tok-1", accept_plan: bool = True, inbox_delay: float = 0.0):
        self.token = token
        self.accept_plan = accept_plan
        self.inbox_delay = inbox_delay
        self.requests: list[dict] = []
        self.messages: list[dict] = []
        self.board = {"project": {"id": "p-1", "title": "Demo", "repo": "", "status": "active"},
                      "tasks": [{"id": "p-1-T1", "key": "T1", "title": "Scaffold", "status": "done", "progress": 100,
                                 "assignee": {"worker_id": "w1", "laptop": "lap1", "agent": "mock"}, "depends_on": []},
                                {"id": "p-1-T2", "key": "T2", "title": "Timer", "status": "in_progress", "progress": 40,
                                 "progress_note": "core logic", "assignee": None, "depends_on": ["p-1-T1"]}]}
        self.port = free_port()
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code: int, payload: dict):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method: str):
                url = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else None
                hub.requests.append({"method": method, "path": url.path, "query": parse_qs(url.query),
                                     "body": body, "auth": self.headers.get("Authorization")})
                if self.headers.get("Authorization") != f"Bearer {hub.token}":
                    return self._reply(401, {"error": {"code": "unauthorized", "message": "bad token"}})
                if url.path == "/api/agent/say":
                    msg = {"id": f"m-{len(hub.messages) + 1:06d}", "text": body["text"], "to": body.get("to", "all"),
                           "from": {"kind": "agent", "task_id": "me", "name": "me"}, "ts": time.time()}
                    hub.messages.append(msg)
                    return self._reply(200, msg)
                if url.path == "/api/agent/inbox":
                    if hub.inbox_delay:
                        time.sleep(hub.inbox_delay)
                    since = (parse_qs(url.query).get("since") or [""])[0]
                    inbox = [{"id": "m-000101", "ts": 1.0, "text": "T2: please export useTimer()", "to": "p-1-T1",
                              "from": {"kind": "agent", "task_id": "p-1-T2", "name": "T2 · codex@lap2"}},
                             {"id": "m-000102", "ts": 2.0, "text": "looks great", "to": "all",
                              "from": {"kind": "human", "name": "carlo"}}]
                    new = [m for m in inbox if m["id"] > since] if since else inbox
                    return self._reply(200, {"messages": new, "cursor": new[-1]["id"] if new else since})
                if url.path == "/api/agent/progress":
                    return self._reply(200, {"ok": True})
                if url.path == "/api/agent/board":
                    return self._reply(200, hub.board)
                if url.path == "/api/agent/plan":
                    if not hub.accept_plan:
                        return self._reply(404, {"error": {"code": "not_found", "message": "no such route"}})
                    return self._reply(200, {"ok": True})
                return self._reply(404, {"error": {"code": "not_found", "message": url.path}})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def paths(self, method: str | None = None) -> list[str]:
        return [r["path"] for r in self.requests if method is None or r["method"] == method]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub_hub():
    hubs: list[StubHub] = []

    def make(**kw) -> StubHub:
        hub = StubHub(**kw)
        hubs.append(hub)
        return hub

    yield make
    for h in hubs:
        h.close()
