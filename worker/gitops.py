"""
Git / GitHub operations for task workspaces (PROTOCOL.md §5 bootstrap, §6).

Everything runs through asyncio subprocesses with argv lists (never a shell)
and reports each meaningful step as a `git` transcript event through the
`emit(kind, text, data)` callback.

GitHub mode (project.repo = "owner/name"):
    clone_or_fetch -> checkout_task_branch -> (agent works, commits)
    -> commit_all (leftovers) -> sync_with_base (rebase, else merge; may report a conflict)
    -> push_branch -> create_pr -> merge_pr (AUTO_MERGE)
    plus bootstrap_base() for empty repos (PLAN task).
Local mode (repo = ""):
    init_local() once per project per laptop; tasks just commit there.

Env vars:
    GH_BIN    gh executable (default "gh"; tests point it at a fake script)
    GIT_BIN   git executable (default "git")
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Callable

EmitGit = Callable[[str, str, dict | None], None]
NET_TIMEOUT = 300   # clone / fetch / push / gh calls
LOCAL_TIMEOUT = 60  # everything else
FALLBACK_NAME = "OnlyVibes Worker"
FALLBACK_EMAIL = "worker@onlyvibes.local"
# Build/test caches agents leave behind (seen live: __pycache__/*.pyc swept into a commit).
# Listed in .git/info/exclude — local to the clone, never committed — so the worker's
# leftover `git add -A` can't commit them even when the project has no .gitignore.
LOCAL_EXCLUDES = ["__pycache__/", "*.py[cod]", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
                  ".venv/", "node_modules/", ".DS_Store"]
_URL = re.compile(r"https://\S+/pull/\d+")
_MARKER = re.compile(r"^(<{7}|>{7})( |$)", re.M)


class GitError(RuntimeError):
    def __init__(self, msg: str, result: "Result | None" = None):
        super().__init__(msg)
        self.result = result


@dataclass
class Result:
    argv: list[str]
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def tail(self, n: int = 400) -> str:
        """The end of stderr (else stdout) without git's multi-line `hint:` advice."""
        text = self.err.strip() or self.out.strip()
        meaningful = "\n".join(ln for ln in text.splitlines() if not ln.startswith("hint:")).strip()
        return (meaningful or text)[-n:]


@dataclass
class SyncResult:
    status: str                      # "ok" | "conflict"
    method: str = ""                 # "rebase" | "merge" | "up-to-date"
    files: list[str] = field(default_factory=list)


_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str, limit: int = 40) -> str:
    slug = _SLUG.sub("-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "task"


def branch_name(task: dict) -> str:
    """ov/<task id lowercased>-<slug of title, ≤ 40 chars>."""
    return f"ov/{task['id'].lower()}-{slugify(task.get('title', ''))}"


class GitOps:
    def __init__(self, emit: EmitGit | None = None, gh_bin: str | None = None,
                 git_bin: str | None = None, env: dict | None = None):
        self.emit = emit or (lambda kind, text, data=None: None)
        self.gh_bin = gh_bin or os.environ.get("GH_BIN") or "gh"
        self.git_bin = git_bin or os.environ.get("GIT_BIN") or "git"
        base_env = dict(os.environ if env is None else env)
        base_env.update({"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "GIT_EDITOR": "true",
                         "GH_NO_UPDATE_NOTIFIER": "1", "LC_ALL": "C"})
        self.env = base_env

    # ------------------------------------------------------------ plumbing

    async def run(self, argv: list[str], cwd: str | None, *, check: bool = True, quiet: bool = False,
                  silent: bool = False, timeout: float = LOCAL_TIMEOUT, label: str | None = None) -> Result:
        """Run one command; raises GitError if check and rc != 0.

        Emits a `git` event for it unless quiet (quiet commands still report failures);
        silent commands never emit (probes and clean-ups that are expected to fail).
        """
        started = time.time()
        shown = label or shlex.join([os.path.basename(argv[0])] + argv[1:])
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=self.env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError as e:
            res = Result(argv, 127, "", str(e))
            self.emit("git", f"{shown} → not found: {argv[0]}", {"rc": 127})
            if check:
                raise GitError(f"{argv[0]} not found", res)
            return res
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except BaseException:  # timeout or cancellation: don't leave git running
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise
        res = Result(argv, proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"))
        secs = time.time() - started
        if not silent and (not quiet or not res.ok):
            if res.ok:
                self.emit("git", f"{shown} → ok ({secs:.1f}s)", {"rc": 0})
            else:
                self.emit("git", f"{shown} → failed (exit {res.rc}): {res.tail(300)}", {"rc": res.rc})
        if check and not res.ok:
            raise GitError(f"{shown} failed: {res.tail()}", res)
        return res

    async def git(self, cwd: str, *args: str, **kw) -> Result:
        return await self.run([self.git_bin, *args], cwd, **kw)

    async def gh(self, cwd: str | None, *args: str, **kw) -> Result:
        kw.setdefault("timeout", NET_TIMEOUT)
        return await self.run([self.gh_bin, *args], cwd, **kw)

    # ------------------------------------------------------------ setup

    async def ensure_identity(self, path: str) -> None:
        """Make sure commits work even on a laptop without a global git identity."""
        for key, fallback in (("user.name", FALLBACK_NAME), ("user.email", FALLBACK_EMAIL)):
            res = await self.git(path, "config", "--get", key, check=False, quiet=True)
            if not res.out.strip():
                await self.git(path, "config", key, fallback, quiet=True)

    @staticmethod
    def ensure_excludes(path: str) -> None:
        """Append LOCAL_EXCLUDES missing from .git/info/exclude (idempotent, best effort)."""
        exclude = os.path.join(path, ".git", "info", "exclude")
        try:
            os.makedirs(os.path.dirname(exclude), exist_ok=True)
            have = open(exclude).read().splitlines() if os.path.exists(exclude) else []
            missing = [p for p in LOCAL_EXCLUDES if p not in have]
            if missing:
                with open(exclude, "a") as fh:
                    fh.write("\n# OnlyVibes worker: build caches\n" + "\n".join(missing) + "\n")
        except OSError:
            pass

    async def init_local(self, path: str, base: str, readme: str) -> bool:
        """Local mode: create the shared per-project repo once. Returns True if created."""
        if os.path.isdir(os.path.join(path, ".git")):
            await self.ensure_identity(path)
            self.ensure_excludes(path)
            return False
        os.makedirs(path, exist_ok=True)
        await self.git(path, "init", "-q")
        await self.git(path, "symbolic-ref", "HEAD", f"refs/heads/{base}", quiet=True)
        await self.ensure_identity(path)
        self.ensure_excludes(path)
        with open(os.path.join(path, "README.md"), "w") as fh:
            fh.write(readme)
        await self.git(path, "add", "README.md", quiet=True)
        await self.git(path, "commit", "-q", "-m", "Initial commit (OnlyVibes local mode)")
        return True

    async def clone_or_fetch(self, repo: str, path: str) -> None:
        if os.path.isdir(os.path.join(path, ".git")):
            await self.git(path, "fetch", "--prune", "origin", timeout=NET_TIMEOUT)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            res = await self.gh(None, "repo", "clone", repo, path, check=False)
            if not res.ok:
                if os.path.isdir(path) and not os.listdir(path):
                    os.rmdir(path)
                # gh missing or not logged in: plain git over https still works for public repos
                await self.git(os.path.dirname(path), "clone", f"https://github.com/{repo}.git", path,
                               timeout=NET_TIMEOUT)
            await self.git(path, "fetch", "--prune", "origin", timeout=NET_TIMEOUT, quiet=True)
        await self.ensure_identity(path)
        self.ensure_excludes(path)

    async def remote_branch_exists(self, path: str, branch: str) -> bool:
        res = await self.git(path, "ls-remote", "--heads", "origin", branch, check=False, quiet=True,
                             timeout=NET_TIMEOUT)
        return res.ok and bool(res.out.strip())

    async def clean_tree(self, path: str) -> None:
        """Abort half-finished operations and drop leftovers from a previous attempt."""
        for op in (("rebase", "--abort"), ("merge", "--abort"), ("cherry-pick", "--abort")):
            await self.git(path, *op, check=False, silent=True)  # fails harmlessly when nothing is in progress
        await self.git(path, "reset", "-q", "--hard", check=False, quiet=True)
        await self.git(path, "clean", "-fdq", check=False, quiet=True)

    async def checkout_task_branch(self, path: str, branch: str, base: str) -> None:
        await self.clean_tree(path)
        await self.git(path, "checkout", "-q", "-B", branch, f"origin/{base}")

    async def checkout_base(self, path: str, base: str) -> None:
        await self.clean_tree(path)
        await self.git(path, "checkout", "-q", "-B", base, f"origin/{base}")

    async def bootstrap_base(self, path: str, base: str, files: dict[str, str], message: str) -> None:
        """Empty GitHub repo: create `base` with an initial commit and push it."""
        await self.git(path, "checkout", "-q", "--orphan", base, check=False, quiet=True)
        await self.git(path, "symbolic-ref", "HEAD", f"refs/heads/{base}", quiet=True)
        for rel, content in files.items():
            full = os.path.join(path, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(content)
        await self.git(path, "add", "--", *files.keys(), quiet=True)
        await self.git(path, "commit", "-q", "-m", message)
        await self.git(path, "push", "-u", "origin", base, timeout=NET_TIMEOUT)

    async def commit_files_to_base(self, path: str, base: str, files: dict[str, str], message: str,
                                   push: bool = True) -> bool:
        """Add a few files on `base` (e.g. PLAN.md after bootstrapping) and push. Best effort."""
        try:
            for rel, content in files.items():
                full = os.path.join(path, rel)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w") as fh:
                    fh.write(content)
            await self.git(path, "add", "--", *files.keys(), quiet=True)
            # probe: exit 1 just means "something is staged", not a failure worth showing
            if not (await self.git(path, "diff", "--cached", "--quiet", check=False, silent=True)).rc:
                return True  # nothing new
            await self.git(path, "commit", "-q", "-m", message)
            if push:
                await self.git(path, "push", "origin", f"HEAD:{base}", timeout=NET_TIMEOUT)
            return True
        except GitError:
            return False

    # ------------------------------------------------------------ commits

    async def is_dirty(self, path: str) -> bool:
        res = await self.git(path, "status", "--porcelain", quiet=True)
        return bool(res.out.strip())

    async def commit_all(self, path: str, message: str) -> bool:
        """`git add -A && git commit` if there is anything to commit. Returns True if committed."""
        if not await self.is_dirty(path):
            return False
        await self.git(path, "add", "-A", quiet=True)
        await self.git(path, "commit", "-q", "-m", message)
        return True

    async def commits_ahead(self, path: str, base: str) -> int:
        res = await self.git(path, "rev-list", "--count", f"origin/{base}..HEAD", check=False, quiet=True)
        try:
            return int(res.out.strip())
        except ValueError:
            return 0

    async def head(self, path: str) -> str:
        res = await self.git(path, "rev-parse", "HEAD", check=False, quiet=True)
        return res.out.strip()

    # ------------------------------------------------------------ syncing

    async def unmerged_files(self, path: str) -> list[str]:
        res = await self.git(path, "diff", "--name-only", "--diff-filter=U", check=False, quiet=True)
        return [f for f in res.out.splitlines() if f.strip()]

    async def conflicted_files(self, path: str) -> list[str]:
        """Files that still contain conflict markers (unmerged or changed since HEAD).

        An unmerged path whose markers are gone counts as resolved (it just
        hasn't been `git add`-ed yet); conclude_merge() stages it.
        """
        changed = await self.git(path, "diff", "--name-only", "HEAD", check=False, quiet=True)
        candidates = dict.fromkeys(await self.unmerged_files(path) + changed.out.splitlines())
        files = []
        for rel in candidates:
            full = os.path.join(path, rel)
            if not rel.strip() or not os.path.isfile(full):
                continue
            try:
                with open(full, errors="ignore") as fh:
                    if _MARKER.search(fh.read()):
                        files.append(rel)
            except OSError:
                pass
        return files

    async def sync_with_base(self, path: str, base: str) -> SyncResult:
        """fetch + rebase onto origin/base; on conflict fall back to a merge.

        On a merge conflict the merge is LEFT IN PROGRESS (markers in the files)
        so an agent can resolve it; call conclude_merge() or abort_merge() next.
        """
        await self.git(path, "fetch", "origin", timeout=NET_TIMEOUT)
        behind = await self.git(path, "rev-list", "--count", f"HEAD..origin/{base}", check=False, quiet=True)
        if behind.out.strip() == "0":
            return SyncResult("ok", "up-to-date")
        res = await self.git(path, "rebase", f"origin/{base}", check=False)
        if res.ok:
            return SyncResult("ok", "rebase")
        await self.git(path, "rebase", "--abort", check=False, silent=True)
        res = await self.git(path, "merge", "--no-edit", f"origin/{base}", check=False)
        if res.ok:
            return SyncResult("ok", "merge")
        files = await self.unmerged_files(path)
        if not files:  # failed for another reason (e.g. dirty tree)
            raise GitError(f"merge with origin/{base} failed: {res.tail()}", res)
        self.emit("git", f"merge conflict with origin/{base} in: {', '.join(files)}", {"files": files})
        return SyncResult("conflict", "merge", files)

    async def merge_in_progress(self, path: str) -> bool:
        res = await self.git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False, silent=True)
        return res.ok

    async def conclude_merge(self, path: str, message: str) -> bool:
        """After an agent resolved conflicts: verify, then commit the merge. False if still conflicted."""
        remaining = await self.conflicted_files(path)
        if remaining:
            self.emit("git", f"still conflicted: {', '.join(remaining)}", {"files": remaining})
            return False
        if await self.merge_in_progress(path):
            await self.git(path, "add", "-A", quiet=True)
            await self.git(path, "commit", "-q", "--no-edit", "-m", message)
        elif await self.is_dirty(path):
            await self.commit_all(path, message)
        return True

    async def abort_merge(self, path: str) -> None:
        await self.git(path, "merge", "--abort", check=False, silent=True)

    # ------------------------------------------------------------ GitHub

    async def push_branch(self, path: str, branch: str) -> None:
        # The task branch is owned by this task; a retried attempt may have rewritten it.
        await self.git(path, "push", "--force-with-lease", "-u", "origin", f"{branch}:{branch}",
                       timeout=NET_TIMEOUT)

    async def create_pr(self, path: str, base: str, branch: str, title: str, body: str) -> str:
        res = await self.gh(path, "pr", "create", "--base", base, "--head", branch,
                            "--title", title, "--body", body, check=False)
        m = _URL.findall(res.out + "\n" + res.err)
        if m:
            return m[-1]
        # e.g. "a pull request for branch … already exists" without a URL: look it up
        view = await self.gh(path, "pr", "view", branch, "--json", "url", "-q", ".url", check=False, quiet=True)
        if view.ok and _URL.search(view.out):
            url = _URL.search(view.out).group(0)
            self.emit("git", f"using existing PR {url}", None)
            return url
        raise GitError(f"gh pr create failed: {res.tail()}", res)

    async def pr_state(self, path: str, url: str) -> str:
        res = await self.gh(path, "pr", "view", url, "--json", "state", "-q", ".state", check=False, quiet=True)
        return res.out.strip().upper() if res.ok else ""

    async def merge_pr(self, path: str, url: str) -> bool:
        """gh pr merge --squash --delete-branch; True if the PR ends up merged."""
        res = await self.gh(path, "pr", "merge", url, "--squash", "--delete-branch", check=False)
        if res.ok:
            return True
        # gh can fail on the local cleanup after a successful merge: ask GitHub
        return await self.pr_state(path, url) == "MERGED"
