"""gitops against local bare repos standing in for GitHub, with a fake `gh` that records calls."""
from __future__ import annotations

import asyncio
import os

import pytest

from conftest import git
from gitops import GitError, GitOps, branch_name, slugify


def run(coro):
    return asyncio.run(coro)


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, kind, text, data=None):
        self.events.append((kind, text))

    def texts(self):
        return "\n".join(t for _, t in self.events)


def test_branch_name_and_slug():
    assert branch_name({"id": "p-3f9a-T3", "title": "Timer state machine + persistence!"}) == \
        "ov/p-3f9a-t3-timer-state-machine-persistence"
    assert len(slugify("x" * 100)) == 40
    assert slugify("!!!") == "task"


def test_normal_build_flow(fake_gh, tmp_path):
    fake_gh.bare("team/app")
    rec = Recorder()
    g = GitOps(emit=rec)
    ws = str(tmp_path / "ws" / "T1")
    branch = "ov/p-1-t1-add-feature"

    async def flow():
        await g.clone_or_fetch("team/app", ws)
        assert await g.remote_branch_exists(ws, "main")
        await g.checkout_task_branch(ws, branch, "main")
        with open(os.path.join(ws, "feature.txt"), "w") as fh:
            fh.write("feature\n")
        assert await g.commit_all(ws, "T1: add feature")
        assert not await g.commit_all(ws, "nothing")  # clean now
        assert await g.commits_ahead(ws, "main") == 1
        sync = await g.sync_with_base(ws, "main")
        assert sync.status == "ok" and sync.method == "up-to-date"
        await g.push_branch(ws, branch)
        url = await g.create_pr(ws, "main", branch, "T1: Add feature", "body text")
        assert url == "https://github.com/team/app/pull/1"
        # creating it again finds the existing PR instead of failing
        assert await g.create_pr(ws, "main", branch, "T1: Add feature", "body text") == url
        assert await g.merge_pr(ws, url)
        return url

    url = run(flow())
    assert fake_gh.show("team/app", "main", "feature.txt") == "feature\n"
    calls = fake_gh.calls()
    assert calls[0][:3] == ["repo", "clone", "team/app"]
    create = next(c for c in calls if c[:2] == ["pr", "create"])
    assert create[create.index("--base") + 1] == "main" and create[create.index("--head") + 1] == branch
    assert create[create.index("--title") + 1] == "T1: Add feature"
    assert ["pr", "merge", url, "--squash", "--delete-branch"] in calls
    assert "git push --force-with-lease -u origin" in rec.texts()
    assert all(kind == "git" for kind, _ in rec.events)


def test_rebase_onto_moved_base(fake_gh, tmp_path):
    fake_gh.bare("team/app")
    g = GitOps()
    a, b = str(tmp_path / "A"), str(tmp_path / "B")

    async def flow():
        for ws, name in ((a, "a"), (b, "b")):
            await g.clone_or_fetch("team/app", ws)
            await g.checkout_task_branch(ws, f"ov/{name}", "main")
            with open(os.path.join(ws, f"{name}.txt"), "w") as fh:
                fh.write(name)
            await g.commit_all(ws, f"{name}: file")
        await g.push_branch(a, "ov/a")
        url = await g.create_pr(a, "main", "ov/a", "A", "")
        assert await g.merge_pr(a, url)
        sync = await g.sync_with_base(b, "main")
        assert sync.status == "ok" and sync.method == "rebase"
        assert os.path.exists(os.path.join(b, "a.txt"))

    run(flow())


def test_conflict_detection_and_resolution(fake_gh, tmp_path):
    fake_gh.bare("team/app")
    rec = Recorder()
    g = GitOps(emit=rec)
    a, b = str(tmp_path / "A"), str(tmp_path / "B")

    async def flow():
        for ws, name in ((a, "a"), (b, "b")):
            await g.clone_or_fetch("team/app", ws)
            await g.checkout_task_branch(ws, f"ov/{name}", "main")
            with open(os.path.join(ws, "README.md"), "w") as fh:
                fh.write(f"# App\n\nline one by {name}\n")
            await g.commit_all(ws, f"{name}: edit readme")
        await g.push_branch(a, "ov/a")
        assert await g.merge_pr(a, await g.create_pr(a, "main", "ov/a", "A", ""))

        sync = await g.sync_with_base(b, "main")
        assert sync.status == "conflict" and sync.files == ["README.md"]
        assert await g.merge_in_progress(b)
        # unresolved -> refuse to conclude
        assert not await g.conclude_merge(b, "b: merge")
        # an "agent" resolves the markers but forgets to commit
        with open(os.path.join(b, "README.md"), "w") as fh:
            fh.write("# App\n\nline one by a\nline one by b\n")
        assert await g.conflicted_files(b) == []
        assert await g.conclude_merge(b, "b: merge main and resolve conflicts")
        assert not await g.merge_in_progress(b) and not await g.is_dirty(b)
        await g.push_branch(b, "ov/b")
        assert await g.merge_pr(b, await g.create_pr(b, "main", "ov/b", "B", ""))

    run(flow())
    assert fake_gh.show("team/app", "main", "README.md") == "# App\n\nline one by a\nline one by b\n"
    assert "merge conflict with origin/main in: README.md" in rec.texts()


def test_abort_merge_restores_clean_tree(fake_gh, tmp_path):
    fake_gh.bare("team/app")
    g = GitOps()
    a, b = str(tmp_path / "A"), str(tmp_path / "B")

    async def flow():
        for ws, name in ((a, "a"), (b, "b")):
            await g.clone_or_fetch("team/app", ws)
            await g.checkout_task_branch(ws, f"ov/{name}", "main")
            with open(os.path.join(ws, "README.md"), "w") as fh:
                fh.write(name)
            await g.commit_all(ws, name)
        await g.push_branch(a, "ov/a")
        await g.merge_pr(a, await g.create_pr(a, "main", "ov/a", "A", ""))
        assert (await g.sync_with_base(b, "main")).status == "conflict"
        await g.abort_merge(b)
        assert not await g.merge_in_progress(b) and not await g.is_dirty(b)

    run(flow())


def test_merge_retry_after_failure(fake_gh, tmp_path, monkeypatch):
    fake_gh.bare("team/app")
    monkeypatch.setenv("FAKE_GH_MERGE_FAILS", "1")
    g = GitOps()
    ws = str(tmp_path / "ws")

    async def flow():
        await g.clone_or_fetch("team/app", ws)
        await g.checkout_task_branch(ws, "ov/x", "main")
        open(os.path.join(ws, "x.txt"), "w").write("x")
        await g.commit_all(ws, "x")
        await g.push_branch(ws, "ov/x")
        url = await g.create_pr(ws, "main", "ov/x", "X", "")
        assert not await g.merge_pr(ws, url)
        assert await g.pr_state(ws, url) == "OPEN"
        assert await g.merge_pr(ws, url)
        assert await g.pr_state(ws, url) == "MERGED"

    run(flow())


def test_empty_repo_bootstrap(fake_gh, tmp_path):
    fake_gh.bare("team/empty", seed=False)
    g = GitOps()
    ws = str(tmp_path / "plan")

    async def flow():
        await g.clone_or_fetch("team/empty", ws)
        assert not await g.remote_branch_exists(ws, "main")
        await g.bootstrap_base(ws, "main", {"README.md": "# Prompt\n"}, "Initial commit")
        assert await g.remote_branch_exists(ws, "main")
        assert await g.commit_files_to_base(ws, "main", {"PLAN.md": "# Plan\n"}, "Add plan")
        # a builder can now branch from it
        b = str(tmp_path / "T1")
        await g.clone_or_fetch("team/empty", b)
        await g.checkout_task_branch(b, "ov/t1", "main")
        assert os.path.exists(os.path.join(b, "PLAN.md"))

    run(flow())
    assert fake_gh.show("team/empty", "main", "README.md") == "# Prompt\n"
    assert fake_gh.show("team/empty", "main", "PLAN.md") == "# Plan\n"


def test_local_mode(tmp_path):
    g = GitOps()
    ws = str(tmp_path / "p-1" / "shared")

    async def flow():
        assert await g.init_local(ws, "main", "# Product\n")
        assert not await g.init_local(ws, "main", "# Product\n")  # only once
        assert git("branch", "--show-current", cwd=ws).strip() == "main"
        before = await g.head(ws)
        open(os.path.join(ws, "a.py"), "w").write("x = 1\n")
        assert await g.commit_all(ws, "T1: a")
        assert await g.head(ws) != before

    run(flow())
    assert "T1: a" in git("log", "--oneline", cwd=ws)


def test_clone_failure_raises(fake_gh, tmp_path, monkeypatch):
    # unknown repo: the fake gh fails, and the https fallback is blocked (no network in tests)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    g = GitOps()
    with pytest.raises(GitError):
        run(g.clone_or_fetch("team/missing", str(tmp_path / "x")))


def test_missing_gh_binary_is_reported(tmp_path):
    rec = Recorder()
    g = GitOps(emit=rec, gh_bin=str(tmp_path / "no-such-gh"))
    res = run(g.gh(None, "pr", "list", check=False))
    assert res.rc == 127 and "not found" in rec.texts()
