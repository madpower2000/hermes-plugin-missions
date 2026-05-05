"""Tests for structured validation report schema and assertion ledger."""
import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp())

from missions import (
    create_mission, approve_mission, start_mission,
    validation_report_template, parse_validation_report,
    update_ledger_from_report, check_mission_passed, mark_mission_passed,
    load_mission, mission_dir
)


@pytest.fixture()
def tmp_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return str(repo)


@pytest.fixture()
def hermes_home(tmp_path):
    return str(tmp_path / "hermes_home")


@pytest.fixture()
def mission_path(tmp_repo, hermes_home):
    os.environ["HERMES_HOME"] = hermes_home
    create_mission(
        goal="Test mission",
        repo_arg=tmp_repo,
        idempotency_key="MISSION-LEDGER-TEST",
    )
    approve_mission("MISSION-LEDGER-TEST")
    start_mission("MISSION-LEDGER-TEST")
    path, meta = load_mission("MISSION-LEDGER-TEST")
    return path


def test_validation_report_template_has_yaml_frontmatter():
    tmpl = validation_report_template("MISSION-TEST", "M1", 1)
    assert "passed_assertions:" in tmpl
    assert "failed_assertions:" in tmpl
    assert "blocking_issues:" in tmpl
    assert "```yaml" in tmpl


def test_parse_validation_report_yaml_frontmatter(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("""---
passed_assertions:
  - VAL-CORE-001
  - VAL-CORE-002
failed_assertions:
  - VAL-CORE-003
blocking_issues:
  - VAL-CORE-003: not working
recommended_fix_task_titles:
  - Fix VAL-CORE-003
---
# Report
""")
    data = parse_validation_report(report)
    assert "VAL-CORE-001" in data["passed_assertions"]
    assert "VAL-CORE-002" in data["passed_assertions"]
    assert "VAL-CORE-003" in data["failed_assertions"]
    assert "Fix VAL-CORE-003" in data["recommended_fix_task_titles"]


def test_parse_validation_report_markdown_fallback(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("""# Report

## Passed assertions
- VAL-CORE-001: works
- VAL-CORE-002: works

## Failed assertions
- VAL-CORE-003: broken

## Blocking issues
- VAL-CORE-003: not working

## recommended fix task titles
- Fix VAL-CORE-003
""")
    data = parse_validation_report(report)
    assert "VAL-CORE-001: works" in data["passed_assertions"]
    assert "VAL-CORE-003: broken" in data["failed_assertions"]
    assert "Fix VAL-CORE-003" in data["recommended_fix_task_titles"]


def test_update_ledger_from_report(mission_path):
    val_dir = mission_path / "validation"
    val_dir.mkdir(exist_ok=True)
    report = val_dir / "M1-round-1.md"
    report.write_text("""---
passed_assertions:
  - VAL-CORE-001
failed_assertions:
  - VAL-CORE-002
blocking_issues:
  - VAL-CORE-002: not working
---
# Report
""")
    ledger = update_ledger_from_report("MISSION-LEDGER-TEST", report)
    assert "VAL-CORE-001" in ledger["assertions"]
    assert ledger["assertions"]["VAL-CORE-001"]["status"] == "passed"
    assert "VAL-CORE-002" in ledger["assertions"]
    assert ledger["assertions"]["VAL-CORE-002"]["status"] == "failed"


def test_ledger_persists_across_rounds(mission_path):
    val_dir = mission_path / "validation"
    val_dir.mkdir(exist_ok=True)

    # Round 1: VAL-001 passed, VAL-002 failed
    report1 = val_dir / "M1-round-1.md"
    report1.write_text("""---
passed_assertions:
  - VAL-CORE-001
failed_assertions:
  - VAL-CORE-002
---
# Report
""")
    update_ledger_from_report("MISSION-LEDGER-TEST", report1)

    # Round 2: VAL-002 also passed
    report2 = val_dir / "M1-round-2.md"
    report2.write_text("""---
passed_assertions:
  - VAL-CORE-001
  - VAL-CORE-002
failed_assertions: []
---
# Report
""")
    ledger = update_ledger_from_report("MISSION-LEDGER-TEST", report2)
    assert ledger["assertions"]["VAL-CORE-001"]["status"] == "passed"
    assert ledger["assertions"]["VAL-CORE-002"]["status"] == "passed"


def test_check_mission_passed_with_failures(mission_path):
    val_dir = mission_path / "validation"
    val_dir.mkdir(exist_ok=True)
    report = val_dir / "M1-round-1.md"
    report.write_text("""---
passed_assertions:
  - VAL-CORE-001
failed_assertions:
  - VAL-CORE-002
---
# Report
""")
    update_ledger_from_report("MISSION-LEDGER-TEST", report)
    check = check_mission_passed("MISSION-LEDGER-TEST")
    assert check["all_passed"] is False
    assert "VAL-CORE-002" in check["failed"]


def test_check_mission_passed_all_pass(mission_path):
    val_dir = mission_path / "validation"
    val_dir.mkdir(exist_ok=True)
    report = val_dir / "M1-round-1.md"
    report.write_text("""---
passed_assertions:
  - VAL-CORE-001
  - VAL-CORE-002
failed_assertions: []
---
# Report
""")
    update_ledger_from_report("MISSION-LEDGER-TEST", report)
    check = check_mission_passed("MISSION-LEDGER-TEST")
    assert check["all_passed"] is True
    assert check["no_blocking_failures"] is True


def test_mark_mission_passed_succeeds(mission_path):
    val_dir = mission_path / "validation"
    val_dir.mkdir(exist_ok=True)
    report = val_dir / "M1-round-1.md"
    report.write_text("""---
passed_assertions:
  - VAL-CORE-001
  - VAL-CORE-002
failed_assertions: []
---
# Report
""")
    update_ledger_from_report("MISSION-LEDGER-TEST", report)
    result = mark_mission_passed("MISSION-LEDGER-TEST")
    assert result["state"] == "passed"


def test_mark_mission_passed_fails_with_blockers(mission_path):
    val_dir = mission_path / "validation"
    val_dir.mkdir(exist_ok=True)
    report = val_dir / "M1-round-1.md"
    report.write_text("""---
passed_assertions:
  - VAL-CORE-001
failed_assertions:
  - VAL-CORE-002
---
# Report
""")
    update_ledger_from_report("MISSION-LEDGER-TEST", report)
    # Mark VAL-CORE-002 as blocking
    path, meta = load_mission("MISSION-LEDGER-TEST")
    ledger_path = path / "validation" / "assertion-ledger.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["assertions"]["VAL-CORE-002"]["blocking"] = True
    ledger_path.write_text(json.dumps(ledger))

    with pytest.raises(ValueError, match="cannot mark passed"):
        mark_mission_passed("MISSION-LEDGER-TEST")
