"""Tests for Factory-style Missions built on Hermes Kanban."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb
import missions


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


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / ".git").mkdir()
    return r


def test_generate_mission_id_is_timestamped_and_slugged():
    mid = missions.generate_mission_id("Build The Big Thing!", now=datetime(2026, 5, 5, 12, 34, 56))
    assert mid == "MISSION-20260505-123456-build-the-big-thing"


def test_create_dry_run_does_not_modify_repo_or_board(isolated_home, repo):
    res = missions.create_mission("Ship dry run", repo_arg=str(repo), dry_run=True)
    assert res.planned is True
    assert not (repo / ".missions").exists()
    assert not kb.kanban_db_path("missions").exists()
    assert res.meta["root_task_id"] is None


def test_create_writes_required_artifacts_and_root_task(isolated_home, repo):
    res = missions.create_mission("Ship mission MVP", repo_arg=str(repo), idempotency_key="MISSION-TEST")
    path = repo / ".missions" / "MISSION-TEST"
    assert res.path == path
    for rel in [
        "mission.yaml", "mission.md", "validation-contract.md", "features.json",
        "milestones.yaml", "services.yaml", "AGENTS.md", "knowledge.md",
        "status.json", "validation", "handoffs",
    ]:
        assert (path / rel).exists(), rel
    meta = yaml.safe_load((path / "mission.yaml").read_text())
    assert meta["mission_id"] == "MISSION-TEST"
    assert meta["state"] == "draft"
    assert meta["repo"] == str(repo)
    features = json.loads((path / "features.json").read_text())
    assert features["milestones"][0]["features"][0]["id"] == "F001"
    with kb.connect(board="missions") as conn:
        root = kb.get_task(conn, meta["root_task_id"])
    assert root is not None
    assert root.title == "MISSION: Ship mission MVP"
    assert root.assignee == "mission-orchestrator"
    assert root.tenant == "MISSION-TEST"


def test_start_generates_idempotent_task_graph_and_links(isolated_home, repo):
    res = missions.create_mission("Graph mission", repo_arg=str(repo), idempotency_key="MISSION-GRAPH")
    missions.approve_mission("MISSION-GRAPH")
    first = missions.start_mission("MISSION-GRAPH")
    second = missions.start_mission("MISSION-GRAPH")
    assert first["kanban_task_ids"] == second["kanban_task_ids"]
    path, meta = missions.load_mission("MISSION-GRAPH")
    assert set(meta["kanban"].keys()) >= {"milestones", "features", "validators", "gates"}
    feature_id = meta["kanban"]["features"]["F001"]
    validator_id = next(iter(meta["kanban"]["validators"].values()))
    gate_id = meta["kanban"]["gates"]["M1"]
    with kb.connect(board="missions") as conn:
        assert meta["kanban"]["milestones"]["M1"] in kb.parent_ids(conn, feature_id)
        assert feature_id in kb.parent_ids(conn, validator_id)
        assert validator_id in kb.parent_ids(conn, gate_id)


def test_status_derives_feature_progress_from_kanban(isolated_home, repo):
    missions.create_mission("Status mission", repo_arg=str(repo), idempotency_key="MISSION-STATUS")
    missions.approve_mission("MISSION-STATUS")
    missions.start_mission("MISSION-STATUS")
    path, meta = missions.load_mission("MISSION-STATUS")
    feature_id = meta["kanban"]["features"]["F001"]
    with kb.connect(board="missions") as conn:
        kb.complete_task(conn, feature_id, summary="done")
    status = missions.derive_status(meta)
    assert status["feature_progress"]["done"] == 1


def test_retry_blockers_creates_fix_tasks_from_latest_report(isolated_home, repo):
    missions.create_mission("Fix mission", repo_arg=str(repo), idempotency_key="MISSION-FIX")
    missions.approve_mission("MISSION-FIX")
    missions.start_mission("MISSION-FIX")
    path, meta = missions.load_mission("MISSION-FIX")
    report = path / "validation" / "M1-round-1.md"
    report.write_text("## Blocking issues\n- Add missing regression test\n- Fix CLI output\n", encoding="utf-8")
    out = missions.retry_blockers("MISSION-FIX")
    assert len(out["created_fix_tasks"]) == 2
    path, meta = missions.load_mission("MISSION-FIX")
    assert len(meta["kanban"]["fixes"]) == 2


def test_kanban_board_path_is_shared_across_profiles(isolated_home, monkeypatch):
    root = isolated_home
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "mission-orchestrator"))
    orchestrator_path = kb.kanban_db_path("missions")
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "backend-eng"))
    worker_path = kb.kanban_db_path("missions")
    assert orchestrator_path == worker_path
    assert str(orchestrator_path).startswith(str(root))
    assert "/profiles/" not in str(orchestrator_path)


def test_worker_destructive_tools_are_scoped_to_current_task(isolated_home, monkeypatch):
    with kb.connect(board="missions") as conn:
        own = kb.create_task(conn, title="own", assignee="worker")
        other = kb.create_task(conn, title="other", assignee="worker")
        kb.claim_task(conn, own)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "missions")
    monkeypatch.setenv("HERMES_KANBAN_TASK", own)
    from tools import kanban_tools as kt
    data = json.loads(kt._handle_complete({"task_id": other, "summary": "bad"}))
    assert data.get("error")
    assert "refusing to mutate" in data["error"]


def test_orchestrator_kanban_tools_visible_with_kanban_toolset(isolated_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    (isolated_home / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    from tools import kanban_tools as kt
    assert kt._check_kanban_mode() is True



def test_create_mission_records_per_agent_models(isolated_home):
    repo = isolated_home / "repo-models"
    repo.mkdir()
    (repo / ".git").mkdir()
    res = missions.create_mission(
        "Model routed mission",
        repo_arg=str(repo),
        idempotency_key="MISSION-MODELS",
        orchestrator_model="gpt-5.5",
        orchestrator_provider="openai-codex",
        worker_models=["local-qwen-fast", "backend=local-qwen-smart"],
        worker_providers=["local-qwen"],
        validator_models=["local-qwen-fast"],
        validator_providers=["local-qwen"],
    )

    models = res.meta["agent_models"]
    assert models["orchestrator"] == {"model": "gpt-5.5", "provider": "openai-codex"}
    assert models["workers"]["default"] == {"model": "local-qwen-fast", "provider": "local-qwen"}
    assert models["workers"]["backend"] == {"model": "local-qwen-smart"}
    assert models["validators"]["default"] == {"model": "local-qwen-fast", "provider": "local-qwen"}

    mission_yaml = repo / ".missions" / "MISSION-MODELS" / "mission.yaml"
    assert "agent_models:" in mission_yaml.read_text(encoding="utf-8")


def test_agent_model_hints_are_in_task_bodies(isolated_home):
    repo = isolated_home / "repo-hints"
    repo.mkdir()
    (repo / ".git").mkdir()
    res = missions.create_mission(
        "Hint mission",
        repo_arg=str(repo),
        idempotency_key="MISSION-HINTS",
        orchestrator_model="gpt-5.5",
        worker_models=["backend=local-qwen-smart"],
        validator_models=["local-qwen-fast"],
    )
    meta = res.meta
    feature = missions.default_features(meta)["milestones"][0]["features"][0]
    validator = missions.default_features(meta)["milestones"][0]["validators"][0]
    milestone = missions.default_features(meta)["milestones"][0]

    assert "Orchestrator model config: model=gpt-5.5" in missions.root_task_body(meta)
    assert "Worker model config: model=local-qwen-smart" in missions.feature_task_body(meta, milestone, feature)
    assert "Validator model config: model=local-qwen-fast" in missions.validator_task_body(meta, milestone, validator, 1)


def test_profiles_result_includes_model_commands():
    result = missions._mission_profiles_result(
        install=True,
        orchestrator_model="gpt-5.5",
        orchestrator_provider="openai-codex",
        worker_model="local-qwen-fast",
        worker_provider="local-qwen",
        validator_model="local-qwen-fast",
        validator_provider="local-qwen",
    )
    commands = result["commands"]
    assert "hermes -p mission-orchestrator config set model.default gpt-5.5" in commands
    assert "hermes -p mission-orchestrator config set model.provider openai-codex" in commands
    assert "hermes -p backend-eng config set model.default local-qwen-fast" in commands
    assert "hermes -p qa-validator config set model.provider local-qwen" in commands
