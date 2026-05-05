"""Regression tests for Kanban preflight invariants Missions relies on."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    return home


def test_named_board_db_is_shared_across_profile_homes(isolated_home, monkeypatch):
    root = isolated_home
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "mission-orchestrator"))
    kb.create_board("missions")
    with kb.connect(board="missions") as conn:
        tid = kb.create_task(conn, title="visible", assignee="backend-eng")

    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "backend-eng"))
    assert kb.kanban_db_path("missions") == root / "kanban" / "boards" / "missions" / "kanban.db"
    with kb.connect(board="missions") as conn:
        assert kb.get_task(conn, tid) is not None


def test_orchestrator_tool_access_via_configured_kanban_toolset(isolated_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    (isolated_home / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    from tools import kanban_tools as kt
    assert kt._check_kanban_mode() is True


def test_worker_cannot_complete_block_or_heartbeat_sibling(isolated_home, monkeypatch):
    with kb.connect(board="missions") as conn:
        own = kb.create_task(conn, title="own", assignee="worker")
        other = kb.create_task(conn, title="other", assignee="worker")
        kb.claim_task(conn, own)
        kb.claim_task(conn, other)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "missions")
    monkeypatch.setenv("HERMES_KANBAN_TASK", own)
    from tools import kanban_tools as kt
    for handler, args in [
        (kt._handle_complete, {"task_id": other, "summary": "bad"}),
        (kt._handle_block, {"task_id": other, "reason": "bad"}),
        (kt._handle_heartbeat, {"task_id": other, "note": "bad"}),
    ]:
        data = json.loads(handler(args))
        assert data.get("error")
        assert "refusing to mutate" in data["error"]


def test_kanban_guidance_preserves_profile_identity():
    from agent.prompt_builder import KANBAN_GUIDANCE
    assert "does not replace your profile identity from SOUL.md" in KANBAN_GUIDANCE
    assert "validator" in KANBAN_GUIDANCE
    assert "do not implement fixes" in KANBAN_GUIDANCE
