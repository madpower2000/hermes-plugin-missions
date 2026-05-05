"""CLI integration tests for `hermes mission` subcommand."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

CLI = sys.executable  # venv python
PLUGIN_DIR = Path(__file__).resolve().parents[1]
HERMES_REPO = Path(os.environ.get("HERMES_AGENT_REPO", "/home/max/.hermes/hermes-agent"))
MAIN = str(HERMES_REPO / "hermes_cli" / "main.py")


@pytest.fixture()
def tmp_repo(tmp_path):
    """Create a temporary git repo and return its path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return str(repo)


@pytest.fixture()
def hermes_home(tmp_path):
    """Return a fresh HERMES_HOME for isolation."""
    return str(tmp_path / "hermes_home")


def _run(args, *, env_extra=None):
    """Run `python hermes_cli/main.py` with the given args."""
    env = os.environ.copy()
    env.pop("HERMES_KANBAN_TASK", None)
    env.pop("HERMES_KANBAN_BOARD", None)
    env.pop("HERMES_KANBAN_TENANT", None)
    if env_extra:
        env.update(env_extra)
    # Standalone directory-plugin tests use isolated HERMES_HOME values, so
    # install the plugin tree there and seed the allow-list explicitly.
    hermes_home = env.get("HERMES_HOME")
    if hermes_home:
        cfg = Path(hermes_home) / "config.yaml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        plugins_dir = Path(hermes_home) / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        target = plugins_dir / "missions"
        if not target.exists():
            import shutil
            ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache")
            shutil.copytree(PLUGIN_DIR, target, ignore=ignore)
        if not cfg.exists():
            cfg.write_text("plugins:\n  enabled:\n    - missions\n", encoding="utf-8")
    result = subprocess.run(
        [CLI, MAIN] + args,
        capture_output=True,
        text=True,
        cwd=str(HERMES_REPO),
        env=env,
    )
    return result


@pytest.mark.parametrize("subcommand", [
    "init", "create", "plan", "approve", "start",
    "status", "show", "validate", "retry-blockers",
    "block", "unblock", "list", "archive", "doctor", "export",
])
def test_mission_help_exists(subcommand):
    """Every mission subcommand must accept --help."""
    r = _run(["mission", subcommand, "--help"])
    assert r.returncode == 0, f"mission {subcommand} --help failed:\n{r.stderr}"
    assert subcommand in r.stdout.lower() or "usage:" in r.stdout.lower()


def test_mission_create_dry_run_json(tmp_repo, hermes_home):
    """--dry-run --json must not create state and must emit JSON."""
    r = _run(
        ["mission", "create", "Dry run test", "--repo", tmp_repo, "--dry-run", "--json"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    assert r.returncode == 0, f"dry-run failed:\n{r.stderr}"
    data = json.loads(r.stdout)
    assert data.get("dry_run") is True
    assert "mission_id" in data
    assert Path(tmp_repo, ".missions").exists() is False


def test_mission_create_real(tmp_repo, hermes_home):
    """Real create must write artifacts and emit JSON."""
    r = _run(
        ["mission", "create", "Real mission", "--repo", tmp_repo,
         "--idempotency-key", "MISSION-CLI-TEST", "--json"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    assert r.returncode == 0, f"create failed:\n{r.stderr}"
    data = json.loads(r.stdout)
    assert data["dry_run"] is False
    assert data["mission_id"] == "MISSION-CLI-TEST"
    mission_dir = Path(tmp_repo) / ".missions" / "MISSION-CLI-TEST"
    assert mission_dir.exists()
    assert (mission_dir / "mission.yaml").exists()
    assert (mission_dir / "validation-contract.md").exists()
    assert (mission_dir / "features.json").exists()


def test_mission_list_json(tmp_repo, hermes_home):
    """Create a mission then list it."""
    _run(
        ["mission", "create", "Listable mission", "--repo", tmp_repo,
         "--idempotency-key", "MISSION-LIST-TEST"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    r = _run(["mission", "list", "--json"], env_extra={"HERMES_HOME": hermes_home})
    assert r.returncode == 0, f"list failed:\n{r.stderr}"
    data = json.loads(r.stdout)
    assert isinstance(data, list)
    assert any(m.get("mission_id") == "MISSION-LIST-TEST" for m in data)


def test_mission_show_json(tmp_repo, hermes_home):
    """Create a mission then show it."""
    _run(
        ["mission", "create", "Showable mission", "--repo", tmp_repo,
         "--idempotency-key", "MISSION-SHOW-TEST"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    r = _run(
        ["mission", "show", "MISSION-SHOW-TEST", "--json"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    assert r.returncode == 0, f"show failed:\n{r.stderr}"
    data = json.loads(r.stdout)
    assert data["mission"]["mission_id"] == "MISSION-SHOW-TEST"
    assert data["mission"]["state"] == "draft"


def test_mission_approve(tmp_repo, hermes_home):
    """Approve a mission and verify state change."""
    _run(
        ["mission", "create", "Approveable mission", "--repo", tmp_repo,
         "--idempotency-key", "MISSION-APPROVE-TEST"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    r = _run(
        ["mission", "approve", "MISSION-APPROVE-TEST", "--json"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    assert r.returncode == 0, f"approve failed:\n{r.stderr}"
    data = json.loads(r.stdout)
    assert data["state"] == "approved"


def test_mission_start(tmp_repo, hermes_home):
    """Approve then start a mission."""
    _run(
        ["mission", "create", "Startable mission", "--repo", tmp_repo,
         "--idempotency-key", "MISSION-START-TEST"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    _run(
        ["mission", "approve", "MISSION-START-TEST"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    r = _run(
        ["mission", "start", "MISSION-START-TEST", "--json"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    assert r.returncode == 0, f"start failed:\n{r.stderr}"
    data = json.loads(r.stdout)
    assert data["state"] == "running"


def test_mission_status_json(tmp_repo, hermes_home):
    """Status must return JSON with state and task info."""
    _run(
        ["mission", "create", "Status mission", "--repo", tmp_repo,
         "--idempotency-key", "MISSION-STATUS-TEST"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    r = _run(
        ["mission", "status", "MISSION-STATUS-TEST", "--json"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    assert r.returncode == 0, f"status failed:\n{r.stderr}"
    data = json.loads(r.stdout)
    assert "state" in data
    assert "mission_id" in data


def test_mission_block_unblock(tmp_repo, hermes_home):
    """Block and unblock a mission."""
    _run(
        ["mission", "create", "Blockable mission", "--repo", tmp_repo,
         "--idempotency-key", "MISSION-BLOCK-TEST"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    r = _run(
        ["mission", "block", "MISSION-BLOCK-TEST", "Need decision on X", "--json"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    assert r.returncode == 0, f"block failed:\n{r.stderr}"
    assert json.loads(r.stdout)["state"] == "blocked"

    r = _run(
        ["mission", "unblock", "MISSION-BLOCK-TEST", "--json"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    assert r.returncode == 0, f"unblock failed:\n{r.stderr}"
    # Unblock restores to running (or previous non-blocked state)
    assert json.loads(r.stdout)["state"] in {"running", "draft"}


def test_mission_doctor_json(hermes_home):
    """Doctor must run and return JSON."""
    r = _run(
        ["mission", "doctor", "--json"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    assert r.returncode == 0, f"doctor failed:\n{r.stderr}"
    data = json.loads(r.stdout)
    assert "checks" in data


def test_mission_export(tmp_repo, hermes_home, tmp_path):
    """Export mission artifacts to a directory."""
    _run(
        ["mission", "create", "Exportable mission", "--repo", tmp_repo,
         "--idempotency-key", "MISSION-EXPORT-TEST"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    out = str(tmp_path / "export_out")
    r = _run(
        ["mission", "export", "MISSION-EXPORT-TEST", "--output", out],
        env_extra={"HERMES_HOME": hermes_home},
    )
    assert r.returncode == 0, f"export failed:\n{r.stderr}"
    assert Path(out).exists()
    assert (Path(out) / "mission.yaml").exists()


def test_mission_validate(tmp_repo, hermes_home):
    """Approve, start, then validate a mission."""
    _run(
        ["mission", "create", "Validatable mission", "--repo", tmp_repo,
         "--idempotency-key", "MISSION-VALIDATE-TEST"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    _run(
        ["mission", "approve", "MISSION-VALIDATE-TEST"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    _run(
        ["mission", "start", "MISSION-VALIDATE-TEST"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    r = _run(
        ["mission", "validate", "MISSION-VALIDATE-TEST", "--json"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    assert r.returncode == 0, f"validate failed:\n{r.stderr}"
    data = json.loads(r.stdout)
    assert data["state"] == "validating"
    assert data["validation_round"] >= 1


def test_mission_archive(tmp_repo, hermes_home):
    """Archive a mission."""
    _run(
        ["mission", "create", "Archivable mission", "--repo", tmp_repo,
         "--idempotency-key", "MISSION-ARCHIVE-TEST"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    r = _run(
        ["mission", "archive", "MISSION-ARCHIVE-TEST", "--json"],
        env_extra={"HERMES_HOME": hermes_home},
    )
    assert r.returncode == 0, f"archive failed:\n{r.stderr}"
    assert json.loads(r.stdout)["state"] == "archived"
