"""Prompt rendering and plan extraction / validation."""
from __future__ import annotations

import json

import pytest

from mock_agent import mock_plan
from planparse import PlanError, extract_plan, normalize_plan
from prompts import render_board, render_build_prompt, render_conflict_prompt, render_plan_prompt

PROJECT = {"id": "p-3f9a", "prompt": "Build a pomodoro web app with stats", "title": "Pomodoro app",
           "repo": "acme/pomodoro", "base_branch": "main", "summary": "Vite SPA; T1 scaffolds."}
BOARD = [
    {"id": "p-3f9a-T1", "key": "T1", "title": "Scaffold", "status": "done",
     "assignee": {"worker_id": "endermite", "laptop": "endermite", "owner": "carlo", "agent": "claude-code"},
     "depends_on": []},
    {"id": "p-3f9a-T2", "key": "T2", "title": "Timer state machine", "status": "in_progress",
     "assignee": {"worker_id": "kaz", "laptop": "kaz-mbp", "owner": "kaz", "agent": "codex"},
     "depends_on": ["p-3f9a-T1"]},
    {"id": "p-3f9a-T3", "key": "T3", "title": "Stats panel", "status": "backlog", "assignee": None,
     "depends_on": ["p-3f9a-T2"]},
    {"id": "p-3f9a-T4", "key": "T4", "title": "Settings", "status": "todo", "assignee": None,
     "depends_on": ["p-3f9a-T1"]},
]
TASK = {"id": "p-3f9a-T2", "key": "T2", "kind": "build", "title": "Timer state machine",
        "description": "OWNS: src/timer/*. Export `createTimer()`.", "acceptance": ["timer survives reload", "unit tests pass"],
        "depends_on": ["p-3f9a-T1"]}


def test_plan_prompt_empty_repo():
    p = render_plan_prompt(PROJECT, {"id": "p-3f9a-PLAN", "key": "PLAN"}, {"empty": True, "files": ["README.md"]})
    assert PROJECT["prompt"] in p
    assert "4–12" in p and "EMPTY" in p and "foundation/scaffold" in p.lower()
    assert "ov plan" in p and "```json" in p
    assert "OWNS" in p and "depends_on" in p and "acceptance" in p
    assert "acme/pomodoro" in p
    # the embedded example is valid JSON and a valid plan
    block = p.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert normalize_plan(json.loads(block))["tasks"][1]["depends_on"] == ["T1"]


def test_plan_prompt_existing_repo_lists_files_and_board():
    p = render_plan_prompt({**PROJECT, "repo": ""}, {"id": "p-3f9a-PLAN"},
                           {"empty": False, "files": ["package.json", "src/main.ts"]},
                           board=BOARD[:1])
    assert "src/main.ts" in p and "local git repository" in p
    assert "do NOT duplicate" in p and "Scaffold" in p


def test_build_prompt_contents():
    p = render_build_prompt(PROJECT, TASK, BOARD, branch="ov/p-3f9a-t2-timer-state-machine",
                            agent="codex", worker_id="kaz")
    assert "T2 · Timer state machine" in p and "createTimer()" in p
    assert "- timer survives reload" in p
    assert "<== YOU" in p and "codex@kaz-mbp (kaz)" in p
    for cmd in ("ov inbox", "ov board", "ov progress", "ov say --to", "--reply-to"):
        assert cmd in p
    assert "ov/p-3f9a-t2-timer-state-machine" in p and "Do NOT push" in p
    assert 'git commit -m "T2: ' in p
    assert "Stats panel" in p.split("Waiting on YOU")[1].split("Working in parallel")[0]  # T3 depends on T2
    assert "Settings" in p.split("Working in parallel")[1]
    assert "summary" in p.lower()


def test_build_prompt_local_mode():
    p = render_build_prompt({**PROJECT, "repo": ""}, TASK, BOARD, local_mode=True)
    assert "LOCAL MODE" in p and "Don't create branches" in p


def test_conflict_prompt():
    p = render_conflict_prompt(PROJECT, TASK, ["src/app.ts", "README.md"], base="main", branch="ov/x")
    assert "- src/app.ts" in p and "<<<<<<< HEAD" in p and "Do NOT push" in p
    assert "T2: merge main and resolve conflicts" in p


def test_render_board_empty():
    assert render_board([]) == "(the board is empty)"


# ---------------------------------------------------------------- plan parsing


def test_extract_plan_last_fenced_block():
    text = ("Here is a draft:\n```json\n{\"tasks\": [{\"title\": \"old\"}]}\n```\nFinal:\n"
            "```json\n{\"title\": \"X\", \"summary\": \"s\", \"tasks\": [{\"ref\": \"T1\", \"title\": \"a\"},"
            " {\"ref\": \"T2\", \"title\": \"b\", \"depends_on\": [\"T1\"]}]}\n```")
    plan = extract_plan(text)
    assert plan["title"] == "X" and len(plan["tasks"]) == 2


def test_extract_plan_none_and_list_form():
    assert extract_plan("no plan here") is None
    assert extract_plan("```json\nnot json\n```") is None
    assert extract_plan('```\n[{"title": "only"}]\n```')["tasks"][0]["title"] == "only"


def test_normalize_plan_cleans_deps_and_priorities():
    plan = normalize_plan({"tasks": [
        {"title": "A", "priority": 9},
        {"ref": "T2", "title": "B", "depends_on": ["T1", "T2", "T9", 1]},
        {"ref": "T2", "title": "C", "depends_on": "2", "acceptance": "works"},
    ]}, default_title="Proj")
    t = plan["tasks"]
    assert plan["title"] == "Proj"
    assert [x["ref"] for x in t] == ["T1", "T2", "T3"]  # duplicate ref fixed
    assert t[0]["priority"] == 4 and t[1]["priority"] == 2
    assert t[1]["depends_on"] == ["T1"]
    assert t[2]["depends_on"] == ["T2"] and t[2]["acceptance"] == ["works"]


@pytest.mark.parametrize("bad", [{}, {"tasks": []}, {"tasks": [{"title": "  "}]}, {"tasks": ["x"]}])
def test_normalize_plan_rejects(bad):
    with pytest.raises(PlanError):
        normalize_plan(bad)


def test_normalize_plan_caps_at_20():
    assert len(normalize_plan({"tasks": [{"title": f"t{i}"} for i in range(30)]})["tasks"]) == 20


def test_mock_plan_shape():
    for seed in ("p-1-PLAN", "p-2-PLAN", "p-3-PLAN", "p-4-PLAN"):
        plan = normalize_plan(mock_plan("Build a pomodoro timer with weekly stats and dark mode", seed))
        assert 5 <= len(plan["tasks"]) <= 7
        assert plan["tasks"][0]["depends_on"] == []
        assert all(t["depends_on"] for t in plan["tasks"][1:])
        assert "Pomodoro" in plan["title"]
    assert mock_plan("x", "a") == mock_plan("x", "a")  # deterministic per seed
