"""Factory-style Missions built on top of Hermes Kanban.

A Mission is intentionally a thin artifact + task-graph layer. Durable execution
state stays in Kanban; repo-local ``.missions/<mission-id>/`` files hold the
validation contract, feature graph, and long-lived project knowledge.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from hermes_cli import kanban_db as kb
from hermes_constants import get_hermes_home

MISSION_STATES = {
    "draft", "planning", "awaiting_approval", "approved", "running",
    "validating", "blocked", "passed", "failed", "archived",
}
DEFAULT_BOARD = "missions"
DEFAULT_ORCHESTRATOR = "mission-orchestrator"
DEFAULT_VALIDATOR = "validator"
DEFAULT_WORKER_ROLE = "worker"


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(text: str, *, max_len: int = 36) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "mission").lower()).strip("-")
    slug = re.sub(r"-+", "-", slug) or "mission"
    return slug[:max_len].strip("-") or "mission"


def generate_mission_id(goal: str, *, now: Optional[datetime] = None) -> str:
    dt = now or datetime.now()
    return f"MISSION-{dt.strftime('%Y%m%d-%H%M%S')}-{slugify(goal)}"


def _split_workspace(value: str, repo: Path) -> tuple[str, Optional[str], str]:
    v = (value or f"dir:{repo}").strip()
    if v in {"scratch", "worktree"}:
        return v, None, v
    if v.startswith("dir:"):
        p = Path(v[4:]).expanduser()
        if not p.is_absolute():
            p = (repo / p).resolve()
        return "dir", str(p), f"dir:{p}"
    raise ValueError("workspace must be scratch, worktree, or dir:<path>")


def _priority_value(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return {"low": 0, "medium": 10, "normal": 10, "high": 50, "urgent": 100}.get(value.lower(), 10)
    return 10


def _priority_label(value: int) -> str:
    return "high" if value >= 50 else "medium" if value >= 10 else "low"


def _ensure_repo(repo_arg: Optional[str]) -> Path:
    if repo_arg:
        repo = Path(repo_arg).expanduser().resolve()
    else:
        cwd = Path.cwd().resolve()
        probe = cwd
        repo = cwd
        while True:
            if (probe / ".git").exists() or (probe / "pyproject.toml").exists() or (probe / "package.json").exists():
                repo = probe
                break
            if probe.parent == probe:
                break
            probe = probe.parent
    if not repo.is_absolute():
        raise ValueError("repo must be an absolute path")
    if not repo.exists() or not repo.is_dir():
        raise ValueError(f"repo path does not exist or is not a directory: {repo}")
    return repo


def missions_root(repo: Path) -> Path:
    return repo / ".missions"


def mission_dir(repo: Path, mission_id: str) -> Path:
    return missions_root(repo) / mission_id


def index_path() -> Path:
    return get_hermes_home() / "missions" / "index.json"


def _load_index() -> dict[str, Any]:
    p = index_path()
    if not p.exists():
        return {"missions": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"missions": {}}
    if not isinstance(data, dict):
        return {"missions": {}}
    data.setdefault("missions", {})
    return data


def _save_index(data: dict[str, Any]) -> None:
    p = index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _index_mission(meta: dict[str, Any], path: Path) -> None:
    data = _load_index()
    data.setdefault("missions", {})[meta["mission_id"]] = {
        "mission_id": meta["mission_id"],
        "title": meta.get("title"),
        "repo": meta.get("repo"),
        "board": meta.get("board"),
        "state": meta.get("state"),
        "path": str(path),
        "updated_at": meta.get("updated_at"),
    }
    _save_index(data)


def _update_index_state(meta: dict[str, Any], path: Path) -> None:
    try:
        _index_mission(meta, path)
    except Exception:
        pass


def find_mission(mission_id: str, *, repo: Optional[Path] = None) -> Path:
    candidates: list[Path] = []
    if repo:
        candidates.append(mission_dir(repo, mission_id))
    cwd = Path.cwd().resolve()
    for p in [cwd, *cwd.parents]:
        candidates.append(p / ".missions" / mission_id)
    idx = _load_index().get("missions", {})
    if mission_id in idx and idx[mission_id].get("path"):
        candidates.append(Path(idx[mission_id]["path"]))
    for c in candidates:
        if (c / "mission.yaml").exists():
            return c
    raise FileNotFoundError(f"mission {mission_id!r} not found (searched current repo and Hermes mission index)")


def load_mission(mission_id: str, *, repo: Optional[Path] = None) -> tuple[Path, dict[str, Any]]:
    path = find_mission(mission_id, repo=repo)
    meta = yaml.safe_load((path / "mission.yaml").read_text(encoding="utf-8")) or {}
    return path, meta


def save_mission(path: Path, meta: dict[str, Any]) -> None:
    meta["updated_at"] = _now_iso()
    (path / "mission.yaml").write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _update_index_state(meta, path)


def _artifact_paths(path: Path) -> dict[str, str]:
    return {
        "validation_contract": str(path / "validation-contract.md"),
        "features": str(path / "features.json"),
        "milestones": str(path / "milestones.yaml"),
        "services": str(path / "services.yaml"),
        "agents": str(path / "AGENTS.md"),
        "knowledge": str(path / "knowledge.md"),
    }


def _parse_worker_map(items: Optional[Iterable[str]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--worker expects role=profile, got {item!r}")
        role, profile = item.split("=", 1)
        role = role.strip()
        profile = profile.strip()
        if not role or not profile:
            raise ValueError(f"--worker expects role=profile, got {item!r}")
        mapping[role] = profile
    if not mapping:
        mapping[DEFAULT_WORKER_ROLE] = "worker"
        mapping["backend"] = "backend-eng"
        mapping["frontend"] = "frontend-eng"
        mapping["qa"] = DEFAULT_VALIDATOR
    return mapping


def _parse_named_values(items: Optional[Iterable[str]], *, option: str, default_key: str = "default") -> dict[str, str]:
    """Parse repeatable NAME=VALUE options, also accepting a bare default VALUE.

    Examples:
      ["local-qwen-fast"] -> {"default": "local-qwen-fast"}
      ["backend=local-qwen-fast", "frontend=gpt-5.4"] -> {"backend": "local-qwen-fast", ...}
    """
    parsed: dict[str, str] = {}
    for item in items or []:
        raw = str(item).strip()
        if not raw:
            continue
        if "=" in raw:
            name, value = raw.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name or not value:
                raise ValueError(f"{option} expects name=value or value, got {item!r}")
            parsed[name] = value
        else:
            parsed[default_key] = raw
    return parsed


def _agent_model_entry(model: Optional[str] = None, provider: Optional[str] = None) -> dict[str, str]:
    entry: dict[str, str] = {}
    if model:
        entry["model"] = model
    if provider:
        entry["provider"] = provider
    return entry


def build_agent_model_config(
    *,
    orchestrator_model: Optional[str] = None,
    orchestrator_provider: Optional[str] = None,
    worker_models: Optional[Iterable[str]] = None,
    worker_providers: Optional[Iterable[str]] = None,
    validator_models: Optional[Iterable[str]] = None,
    validator_providers: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Build mission metadata describing desired model/provider per agent role.

    This metadata is persisted with the mission for auditability. Runtime model
    selection is profile-based: the Kanban dispatcher runs `hermes -p <assignee>`;
    configure each assignee profile's `model.default` and `model.provider` with
    `hermes mission profiles --install ...` or equivalent `hermes -p ... config set`.
    """
    cfg: dict[str, Any] = {}
    orch = _agent_model_entry(orchestrator_model, orchestrator_provider)
    if orch:
        cfg["orchestrator"] = orch

    w_models = _parse_named_values(worker_models, option="--worker-model")
    w_providers = _parse_named_values(worker_providers, option="--worker-provider")
    worker_keys = sorted(set(w_models) | set(w_providers))
    if worker_keys:
        cfg["workers"] = {k: _agent_model_entry(w_models.get(k), w_providers.get(k)) for k in worker_keys}

    v_models = _parse_named_values(validator_models, option="--validator-model")
    v_providers = _parse_named_values(validator_providers, option="--validator-provider")
    validator_keys = sorted(set(v_models) | set(v_providers))
    if validator_keys:
        cfg["validators"] = {k: _agent_model_entry(v_models.get(k), v_providers.get(k)) for k in validator_keys}

    return cfg


def _resolve_agent_model(agent_models: dict[str, Any], section: str, key: Optional[str] = None) -> dict[str, str]:
    section_cfg = agent_models.get(section) if isinstance(agent_models, dict) else None
    if not isinstance(section_cfg, dict):
        return {}
    if section == "orchestrator":
        return {k: str(v) for k, v in section_cfg.items() if k in {"model", "provider"} and v}
    if key and isinstance(section_cfg.get(key), dict):
        return {k: str(v) for k, v in section_cfg[key].items() if k in {"model", "provider"} and v}
    if isinstance(section_cfg.get("default"), dict):
        return {k: str(v) for k, v in section_cfg["default"].items() if k in {"model", "provider"} and v}
    return {}


def _format_model_hint(label: str, cfg: dict[str, str]) -> str:
    if not cfg:
        return ""
    bits = []
    if cfg.get("model"):
        bits.append(f"model={cfg['model']}")
    if cfg.get("provider"):
        bits.append(f"provider={cfg['provider']}")
    return f"\n{label} model config: " + ", ".join(bits)


def root_task_body(meta: dict[str, Any]) -> str:
    artifacts = meta["artifacts"]
    return f"""Mission ID: {meta['mission_id']}
Repo: {meta['repo']}
Goal: {meta['goal']}{_format_model_hint('Orchestrator', _resolve_agent_model(meta.get('agent_models') or {}, 'orchestrator'))}
Artifacts:
- validation contract path: {artifacts['validation_contract']}
- features path: {artifacts['features']}
- services path: {artifacts['services']}
- AGENTS path: {artifacts['agents']}
- knowledge path: {artifacts['knowledge']}

Instructions:
1. Confirm requirements.
2. Write validation contract.
3. Write features/milestones.
4. Create linked Kanban feature tasks.
5. Create milestone validation tasks.
6. Monitor validator results.
7. Create fix features for blockers.
8. Mark mission passed only when all assertions pass.
"""


def mission_md(meta: dict[str, Any]) -> str:
    return f"""# {meta['title']}

Mission ID: {meta['mission_id']}
State: {meta['state']}
Repo: {meta['repo']}
Board: {meta['board']}
Root Kanban task: {meta.get('root_task_id') or 'not created yet'}

## Goal

{meta['goal']}

## Lifecycle

- draft: artifacts created, not planned yet
- planning: orchestrator is clarifying requirements / writing contract
- awaiting_approval: validation contract and feature graph are ready for human approval
- approved: ready to start
- running: implementation tasks are dispatchable through Kanban
- validating: validators/gates are checking milestone assertions
- blocked: human decision required
- passed/failed/archived: terminal states
"""


def validation_contract_template(title: str) -> str:
    return f"""# Validation Contract: {title}

## Completion Rule
Mission is complete only when every assertion below passes with evidence.

## Assertions
### VAL-CORE-001: Mission goal satisfied
Behavior:
Evidence required:
Tool/check:
Owner:
Milestone: M1
Blocking: true

## Non-goals
- Add non-goals here.

## Regression checks
- Run relevant existing tests.
- Add targeted tests for new behavior where practical.
"""


def default_features(meta: dict[str, Any]) -> dict[str, Any]:
    worker_profiles = meta.get("worker_profiles") or {DEFAULT_WORKER_ROLE: "worker"}
    worker_profile = worker_profiles.get("backend") or worker_profiles.get(DEFAULT_WORKER_ROLE) or next(iter(worker_profiles.values()))
    repo = meta["repo"]
    workspace = meta.get("workspace", f"dir:{repo}")
    return {
        "mission_id": meta["mission_id"],
        "title": meta["title"],
        "milestones": [
            {
                "id": "M1",
                "title": "Foundation",
                "description": "Implement the first bounded slice that satisfies the core validation assertion.",
                "depends_on": [],
                "validation_assertions": ["VAL-CORE-001"],
                "features": [
                    {
                        "id": "F001",
                        "title": "Implement core mission slice",
                        "description": meta["goal"],
                        "assignee_role": "backend" if "backend" in worker_profiles else DEFAULT_WORKER_ROLE,
                        "assignee_profile": worker_profile,
                        "workspace": workspace,
                        "depends_on": [],
                        "claims_assertions": ["VAL-CORE-001"],
                        "tests_required": True,
                        "risk": "medium",
                    }
                ],
                "validators": [
                    {
                        "id": "V-M1-001",
                        "title": "Validate milestone M1",
                        "assignee_profile": (meta.get("validator_profiles") or [DEFAULT_VALIDATOR])[0],
                        "kind": "scrutiny",
                        "assertions": ["VAL-CORE-001"],
                    }
                ],
            }
        ],
    }


def agents_template(meta: dict[str, Any]) -> str:
    return f"""# Mission operating rules: {meta['title']}

Mission ID: {meta['mission_id']}
Repo: {meta['repo']}

## Scope boundaries
- Implement only the Kanban task you were assigned.
- Stay inside the declared workspace unless the task body explicitly permits otherwise.
- Do not mutate sibling tasks except by comments or explicitly authorized follow-up creation.

## Testing expectations
- Read validation-contract.md before editing.
- Add or update tests where practical.
- Run the narrowest relevant test/check set and record exact commands.

## Validation assertion mapping
- Every worker completion must list assertions claimed.
- Validators must independently verify assertions and must not implement fixes.

## Forbidden actions
- Do not bypass Kanban by spawning an in-process agent swarm.
- Do not mark the mission passed unless every blocking assertion has evidence.
- Do not store secrets or noisy logs in mission artifacts.

## knowledge.md updates
Workers may append durable discoveries only: API quirks, project conventions,
non-obvious decisions, and reproducible environment facts. Do not paste raw logs.

## Completion report format
Complete the Kanban task with a structured JSON-like summary:
- files_changed
- tests_added
- tests_run
- assertions_claimed
- evidence
- risks
- followups
"""


def services_template() -> str:
    return """# Local services needed for validation
# - name:
#   command:
#   cwd:
#   healthcheck:
#   env:
#   notes:
[]
"""


def _write_status(path: Path, summary: dict[str, Any]) -> None:
    (path / "status.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@dataclass
class MissionCreateResult:
    mission_id: str
    path: Path
    meta: dict[str, Any]
    planned: bool = False


def ensure_missions_board(board: str = DEFAULT_BOARD, *, switch: bool = False) -> dict[str, Any]:
    meta = kb.create_board(board, name="Missions" if board == DEFAULT_BOARD else None,
                           description="Factory-style missions" if board == DEFAULT_BOARD else None)
    if switch:
        kb.set_current_board(board)
    return meta


def create_mission(
    goal: str,
    *,
    repo_arg: Optional[str] = None,
    board: str = DEFAULT_BOARD,
    orchestrator: str = DEFAULT_ORCHESTRATOR,
    validators: Optional[list[str]] = None,
    workers: Optional[list[str]] = None,
    workspace: Optional[str] = None,
    tenant: Optional[str] = None,
    priority: int = 50,
    idempotency_key: Optional[str] = None,
    skip_clarification: bool = False,
    dry_run: bool = False,
    orchestrator_model: Optional[str] = None,
    orchestrator_provider: Optional[str] = None,
    worker_models: Optional[list[str]] = None,
    worker_providers: Optional[list[str]] = None,
    validator_models: Optional[list[str]] = None,
    validator_providers: Optional[list[str]] = None,
) -> MissionCreateResult:
    repo = _ensure_repo(repo_arg)
    mission_id = idempotency_key or generate_mission_id(goal)
    title = goal.strip().splitlines()[0][:80].strip() or "Mission"
    path = mission_dir(repo, mission_id)
    validators = validators or [DEFAULT_VALIDATOR]
    worker_map = _parse_worker_map(workers)
    ws_kind, ws_path, ws_display = _split_workspace(workspace or f"dir:{repo}", repo)
    tenant_value = tenant or mission_id
    artifacts = _artifact_paths(path)
    agent_models = build_agent_model_config(
        orchestrator_model=orchestrator_model,
        orchestrator_provider=orchestrator_provider,
        worker_models=worker_models,
        worker_providers=worker_providers,
        validator_models=validator_models,
        validator_providers=validator_providers,
    )
    meta = {
        "mission_id": mission_id,
        "title": title,
        "goal": goal,
        "repo": str(repo),
        "board": board,
        "root_task_id": None,
        "state": "draft",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "orchestrator_profile": orchestrator,
        "worker_profiles": worker_map,
        "validator_profiles": validators,
        "current_milestone": None,
        "validation_round": 0,
        "workspace": ws_display,
        "skip_clarification": bool(skip_clarification),
        "agent_models": agent_models,
        "kanban": {
            "board": board,
            "tenant": tenant_value,
            "root_task_id": None,
            "task_ids": [],
            "milestones": {},
            "features": {},
            "validators": {},
            "gates": {},
            "fixes": {},
        },
        "artifacts": artifacts,
    }
    if dry_run:
        return MissionCreateResult(mission_id=mission_id, path=path, meta=meta, planned=True)

    if path.exists() and idempotency_key:
        existing = yaml.safe_load((path / "mission.yaml").read_text(encoding="utf-8")) or {}
        return MissionCreateResult(mission_id=mission_id, path=path, meta=existing)
    path.mkdir(parents=True, exist_ok=True)
    (path / "validation").mkdir(exist_ok=True)
    (path / "handoffs").mkdir(exist_ok=True)

    ensure_missions_board(board)
    with kb.connect(board=board) as conn:
        root_id = kb.create_task(
            conn,
            title=f"MISSION: {title}",
            body=root_task_body(meta),
            assignee=orchestrator,
            created_by="mission-cli",
            workspace_kind=ws_kind,
            workspace_path=ws_path,
            tenant=tenant_value,
            priority=priority,
            idempotency_key=f"mission:{mission_id}:root",
            skills=["kanban-orchestrator"],
        )
    meta["root_task_id"] = root_id
    meta["kanban"]["root_task_id"] = root_id
    meta["kanban"]["task_ids"] = [root_id]

    (path / "mission.yaml").write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (path / "mission.md").write_text(mission_md(meta), encoding="utf-8")
    (path / "validation-contract.md").write_text(validation_contract_template(title), encoding="utf-8")
    (path / "features.json").write_text(json.dumps(default_features(meta), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (path / "milestones.yaml").write_text(yaml.safe_dump(default_features(meta)["milestones"], sort_keys=False, allow_unicode=True), encoding="utf-8")
    (path / "services.yaml").write_text(services_template(), encoding="utf-8")
    (path / "AGENTS.md").write_text(agents_template(meta), encoding="utf-8")
    (path / "knowledge.md").write_text(f"# Mission knowledge: {title}\n\nDurable discoveries only.\n", encoding="utf-8")
    _write_status(path, derive_status(meta))
    _index_mission(meta, path)
    return MissionCreateResult(mission_id=mission_id, path=path, meta=meta)


def _load_features(path: Path) -> dict[str, Any]:
    p = path / "features.json"
    if not p.exists():
        raise FileNotFoundError(f"missing features.json at {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("milestones"), list):
        raise ValueError("features.json must be an object with a milestones array")
    return data


def _save_features_to_milestones(path: Path, features: dict[str, Any]) -> None:
    (path / "milestones.yaml").write_text(yaml.safe_dump(features.get("milestones", []), sort_keys=False, allow_unicode=True), encoding="utf-8")


def plan_mission(mission_id: str) -> dict[str, Any]:
    path, meta = load_mission(mission_id)
    features = default_features(meta)
    # Preserve user edits if files already contain non-empty structured content.
    try:
        existing = _load_features(path)
        if existing.get("milestones"):
            features = existing
    except Exception:
        pass
    (path / "validation-contract.md").write_text(validation_contract_template(meta["title"]), encoding="utf-8")
    (path / "features.json").write_text(json.dumps(features, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _save_features_to_milestones(path, features)
    meta["state"] = "awaiting_approval"
    board = meta["board"]
    root = meta.get("root_task_id")
    with kb.connect(board=board) as conn:
        planning_id = kb.create_task(
            conn,
            title=f"Plan mission {mission_id}",
            body=planning_task_body(meta),
            assignee=meta["orchestrator_profile"],
            created_by="mission-cli",
            workspace_kind="dir",
            workspace_path=meta["repo"],
            tenant=meta["kanban"]["tenant"],
            priority=40,
            parents=[root] if root else [],
            idempotency_key=f"mission:{mission_id}:planning",
            skills=["kanban-orchestrator"],
        )
        if planning_id not in meta["kanban"].get("task_ids", []):
            meta["kanban"].setdefault("task_ids", []).append(planning_id)
        # Release planning task if root is only a durable tracking marker.
        if root:
            kb.complete_task(conn, root, summary="Mission artifacts initialized; planning task created.")
    save_mission(path, meta)
    summary = derive_status(meta)
    _write_status(path, summary)
    return {"mission_id": mission_id, "path": str(path), "state": meta["state"], "planning_task_id": planning_id}


def planning_task_body(meta: dict[str, Any]) -> str:
    return f"""Mission ID: {meta['mission_id']}
Repo: {meta['repo']}
Goal: {meta['goal']}{_format_model_hint('Orchestrator', _resolve_agent_model(meta.get('agent_models') or {}, 'orchestrator'))}
Artifacts:
- validation contract: {meta['artifacts']['validation_contract']}
- features: {meta['artifacts']['features']}
- milestones: {meta['artifacts']['milestones']}

Instructions:
1. Clarify requirements in comments if the goal is not testable.
2. Update validation-contract.md with blocking assertions.
3. Update features.json with bounded milestones/features/validators.
4. Keep features small enough for local/low-concurrency execution.
5. Complete with evidence that the plan is ready for `hermes mission approve`.
"""


def approve_mission(mission_id: str) -> dict[str, Any]:
    path, meta = load_mission(mission_id)
    meta["state"] = "approved"
    save_mission(path, meta)
    summary = derive_status(meta)
    _write_status(path, summary)
    return summary


def _create_or_get_task(conn, meta: dict[str, Any], *, key: str, title: str, body: str,
                        assignee: str, parents: list[str], priority: int, workspace: str,
                        skills: Optional[list[str]] = None) -> str:
    repo = Path(meta["repo"])
    ws_kind, ws_path, _ = _split_workspace(workspace, repo)
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        created_by="mission-cli",
        workspace_kind=ws_kind,
        workspace_path=ws_path,
        tenant=meta["kanban"]["tenant"],
        priority=priority,
        parents=parents,
        idempotency_key=f"mission:{meta['mission_id']}:{key}",
        skills=skills,
    )


def start_mission(mission_id: str) -> dict[str, Any]:
    path, meta = load_mission(mission_id)
    if meta.get("state") not in {"approved", "running", "validating", "awaiting_approval"}:
        raise ValueError(f"mission must be approved before start (current state: {meta.get('state')})")
    features = _load_features(path)
    board = meta["board"]
    ensure_missions_board(board)
    task_ids = set(meta["kanban"].get("task_ids") or [])
    milestone_ids = meta["kanban"].setdefault("milestones", {})
    feature_ids = meta["kanban"].setdefault("features", {})
    validator_ids = meta["kanban"].setdefault("validators", {})
    gate_ids = meta["kanban"].setdefault("gates", {})
    root = meta.get("root_task_id")
    with kb.connect(board=board) as conn:
        # The root task is a durable mission marker. Mark it done so child dependency
        # links can act as grouping without starving the first milestone forever.
        if root and (t := kb.get_task(conn, root)) and t.status != "done":
            kb.complete_task(conn, root, summary="Mission started; releasing milestone graph.")
        previous_gate: Optional[str] = None
        for milestone in features.get("milestones", []):
            mid = milestone["id"]
            m_parents = [root] if root else []
            for dep in milestone.get("depends_on") or []:
                if dep in gate_ids:
                    m_parents.append(gate_ids[dep])
            if previous_gate and previous_gate not in m_parents and not milestone.get("depends_on"):
                m_parents.append(previous_gate)
            m_task = _create_or_get_task(
                conn, meta,
                key=f"milestone:{mid}",
                title=f"Milestone {mid}: {milestone.get('title', mid)}",
                body=milestone_task_body(meta, milestone),
                assignee=meta["orchestrator_profile"],
                parents=m_parents,
                priority=30,
                workspace=meta.get("workspace", f"dir:{meta['repo']}"),
                skills=["kanban-orchestrator"],
            )
            milestone_ids[mid] = m_task
            task_ids.add(m_task)
            # Grouping marker: complete immediately so implementation features can dispatch.
            if (mt := kb.get_task(conn, m_task)) and mt.status in {"ready", "blocked"}:
                kb.complete_task(conn, m_task, summary=f"Milestone {mid} graph opened.")

            local_feature_tasks: list[str] = []
            for feature in milestone.get("features") or []:
                fid = feature["id"]
                parents = [m_task]
                for dep in feature.get("depends_on") or []:
                    if dep in feature_ids:
                        parents.append(feature_ids[dep])
                f_task = _create_or_get_task(
                    conn, meta,
                    key=f"feature:{fid}",
                    title=f"{fid}: {feature.get('title', fid)}",
                    body=feature_task_body(meta, milestone, feature),
                    assignee=feature.get("assignee_profile") or meta.get("worker_profiles", {}).get(feature.get("assignee_role"), "worker"),
                    parents=parents,
                    priority=_priority_value(feature.get("risk", 10)),
                    workspace=feature.get("workspace") or meta.get("workspace", f"dir:{meta['repo']}"),
                    skills=None,
                )
                feature_ids[fid] = f_task
                local_feature_tasks.append(f_task)
                task_ids.add(f_task)

            round_no = int(meta.get("validation_round") or 1) or 1
            local_validator_tasks: list[str] = []
            for validator in milestone.get("validators") or []:
                vid = f"{validator['id']}-R{round_no}"
                v_task = _create_or_get_task(
                    conn, meta,
                    key=f"validator:{vid}",
                    title=f"{validator.get('title', 'Validate milestone')} (round {round_no})",
                    body=validator_task_body(meta, milestone, validator, round_no),
                    assignee=validator.get("assignee_profile") or (meta.get("validator_profiles") or [DEFAULT_VALIDATOR])[0],
                    parents=local_feature_tasks,
                    priority=60,
                    workspace=meta.get("workspace", f"dir:{meta['repo']}"),
                    skills=None,
                )
                validator_ids[vid] = v_task
                local_validator_tasks.append(v_task)
                task_ids.add(v_task)
            gate_key = f"gate:{mid}:R{round_no}"
            gate = _create_or_get_task(
                conn, meta,
                key=gate_key,
                title=f"Gate {mid} validation round {round_no}",
                body=gate_task_body(meta, milestone, round_no),
                assignee=meta["orchestrator_profile"],
                parents=local_validator_tasks,
                priority=70,
                workspace=meta.get("workspace", f"dir:{meta['repo']}"),
                skills=["kanban-orchestrator"],
            )
            gate_ids[mid] = gate
            previous_gate = gate
            task_ids.add(gate)
        kb.recompute_ready(conn)
    meta["state"] = "running"
    meta["current_milestone"] = (features.get("milestones") or [{}])[0].get("id")
    meta["validation_round"] = max(int(meta.get("validation_round") or 0), 1)
    meta["kanban"]["task_ids"] = sorted(task_ids)
    save_mission(path, meta)
    summary = derive_status(meta)
    _write_status(path, summary)
    return summary


def milestone_task_body(meta: dict[str, Any], milestone: dict[str, Any]) -> str:
    return f"""Mission ID: {meta['mission_id']}
Milestone ID: {milestone.get('id')}
Repo: {meta['repo']}
Description: {milestone.get('description', '')}
Validation assertions: {', '.join(milestone.get('validation_assertions') or [])}

This is a grouping/gate marker for Kanban task links. Implementation happens in child feature tasks.
"""


def feature_task_body(meta: dict[str, Any], milestone: dict[str, Any], feature: dict[str, Any]) -> str:
    return f"""Mission ID: {meta['mission_id']}
Feature ID: {feature.get('id')}
Milestone ID: {milestone.get('id')}
Repo: {meta['repo']}
Workspace: {feature.get('workspace') or meta.get('workspace')}{_format_model_hint('Worker', _resolve_agent_model(meta.get('agent_models') or {}, 'workers', feature.get('assignee_role') or 'default'))}
Relevant files:
Validation assertions claimed: {', '.join(feature.get('claims_assertions') or [])}
Required tests: {feature.get('tests_required', True)}
Scope:
{feature.get('description', '')}
Non-goals:
- Do not implement unrelated features.

Instructions:
1. Read .missions/{meta['mission_id']}/AGENTS.md.
2. Read validation-contract.md and features.json.
3. Implement only this feature.
4. Write/update tests where practical.
5. Run relevant tests.
6. Append durable findings to knowledge.md.
7. Complete with structured JSON-like summary:
   - files_changed
   - tests_added
   - tests_run
   - assertions_claimed
   - evidence
   - risks
   - followups
"""


def validator_task_body(meta: dict[str, Any], milestone: dict[str, Any], validator: dict[str, Any], round_no: int) -> str:
    assertions = validator.get("assertions") or milestone.get("validation_assertions") or []
    return f"""Mission ID: {meta['mission_id']}
Milestone ID: {milestone.get('id')}
Validation round: {round_no}
Repo: {meta['repo']}
Assertions to validate: {', '.join(assertions)}
Validator kind: {validator.get('kind', 'scrutiny')}{_format_model_hint('Validator', _resolve_agent_model(meta.get('agent_models') or {}, 'validators', validator.get('id') or validator.get('kind') or 'default'))}

Instructions:
1. Read validation-contract.md.
2. Read relevant worker results and changed files.
3. Do not implement fixes.
4. Verify behavior with tests, inspection, or black-box checks.
5. Write .missions/{meta['mission_id']}/validation/{milestone.get('id')}-round-{round_no}.md.
6. Complete with:
   - passed_assertions
   - failed_assertions
   - blocking_issues
   - non_blocking_issues
   - reproduction_steps
   - evidence
   - recommended fix task titles
"""


def gate_task_body(meta: dict[str, Any], milestone: dict[str, Any], round_no: int) -> str:
    return f"""Mission ID: {meta['mission_id']}
Milestone ID: {milestone.get('id')}
Validation round: {round_no}{_format_model_hint('Orchestrator', _resolve_agent_model(meta.get('agent_models') or {}, 'orchestrator'))}
Instructions:
1. Read validator reports.
2. If no blocking issues remain, mark milestone passed.
3. If blocking issues exist, create targeted fix feature tasks.
4. Link fix tasks before the next validation task.
5. If blocked by missing human decision, block the mission and explain exact question.
"""


def derive_status(meta: dict[str, Any]) -> dict[str, Any]:
    board = meta.get("board") or DEFAULT_BOARD
    task_ids = meta.get("kanban", {}).get("task_ids") or []
    tasks: list[kb.Task] = []
    try:
        with kb.connect(board=board) as conn:
            kb.recompute_ready(conn)
            for tid in task_ids:
                t = kb.get_task(conn, tid)
                if t:
                    tasks.append(t)
    except Exception:
        tasks = []
    counts: dict[str, int] = {}
    blockers = []
    ready = []
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
        if t.status == "blocked":
            blockers.append({"id": t.id, "title": t.title, "assignee": t.assignee})
        if t.status == "ready":
            ready.append({"id": t.id, "title": t.title, "assignee": t.assignee})
    feature_map = meta.get("kanban", {}).get("features", {}) or {}
    validator_map = meta.get("kanban", {}).get("validators", {}) or {}
    return {
        "mission_id": meta.get("mission_id"),
        "title": meta.get("title"),
        "state": meta.get("state"),
        "current_milestone": meta.get("current_milestone"),
        "validation_round": meta.get("validation_round", 0),
        "board": board,
        "root_task_id": meta.get("root_task_id"),
        "task_counts": counts,
        "feature_progress": _progress(tasks, set(feature_map.values())),
        "validator_progress": _progress(tasks, set(validator_map.values())),
        "blockers": blockers,
        "next_ready_tasks": ready,
        "kanban_task_ids": task_ids,
        "artifacts": meta.get("artifacts", {}),
    }


def _progress(tasks: list[kb.Task], ids: set[str]) -> dict[str, int]:
    selected = [t for t in tasks if t.id in ids]
    done = len([t for t in selected if t.status == "done"])
    blocked = len([t for t in selected if t.status == "blocked"])
    return {"total": len(selected), "done": done, "blocked": blocked, "remaining": max(len(selected) - done, 0)}


def block_mission(mission_id: str, reason: str) -> dict[str, Any]:
    path, meta = load_mission(mission_id)
    meta["state"] = "blocked"
    with kb.connect(board=meta["board"]) as conn:
        root = meta.get("root_task_id")
        if root:
            kb.add_comment(conn, root, "mission-cli", f"Mission blocked: {reason}")
            t = kb.get_task(conn, root)
            if t and t.status in {"ready", "running"}:
                kb.block_task(conn, root, reason=reason)
    save_mission(path, meta)
    summary = derive_status(meta)
    summary["blocked_reason"] = reason
    _write_status(path, summary)
    return summary


def unblock_mission(mission_id: str) -> dict[str, Any]:
    path, meta = load_mission(mission_id)
    meta["state"] = "running" if meta.get("kanban", {}).get("task_ids") else "approved"
    with kb.connect(board=meta["board"]) as conn:
        root = meta.get("root_task_id")
        if root:
            kb.add_comment(conn, root, "mission-cli", "Mission unblocked.")
            kb.unblock_task(conn, root)
    save_mission(path, meta)
    summary = derive_status(meta)
    _write_status(path, summary)
    return summary


def archive_mission(mission_id: str) -> dict[str, Any]:
    path, meta = load_mission(mission_id)
    meta["state"] = "archived"
    with kb.connect(board=meta["board"]) as conn:
        for tid in meta.get("kanban", {}).get("task_ids") or []:
            try:
                kb.archive_task(conn, tid)
            except Exception:
                pass
    save_mission(path, meta)
    summary = derive_status(meta)
    _write_status(path, summary)
    return summary


def validate_mission(mission_id: str, milestone_id: Optional[str] = None) -> dict[str, Any]:
    path, meta = load_mission(mission_id)
    meta["state"] = "validating"
    meta["validation_round"] = int(meta.get("validation_round") or 0) + 1
    features = _load_features(path)
    created = []
    with kb.connect(board=meta["board"]) as conn:
        for milestone in features.get("milestones", []):
            if milestone_id and milestone.get("id") != milestone_id:
                continue
            feature_tasks = [meta["kanban"].get("features", {}).get(f.get("id")) for f in milestone.get("features") or []]
            feature_tasks = [x for x in feature_tasks if x]
            # Validation rounds after a failed gate must wait for any targeted
            # fix features generated from validator blockers. Keep this thin:
            # fixes remain normal Kanban tasks; validation simply depends on
            # the current set of mission fix task IDs so blockers are resolved
            # before the next validator run becomes dispatchable.
            fix_tasks = list((meta.get("kanban", {}).get("fixes") or {}).values())
            validation_parents = sorted(set(feature_tasks + fix_tasks))
            if not validation_parents:
                validation_parents = feature_tasks
            for validator in milestone.get("validators") or []:
                vid = f"{validator['id']}-R{meta['validation_round']}"
                task_id = _create_or_get_task(
                    conn, meta,
                    key=f"validator:{vid}",
                    title=f"{validator.get('title', 'Validate milestone')} (round {meta['validation_round']})",
                    body=validator_task_body(meta, milestone, validator, meta["validation_round"]),
                    assignee=validator.get("assignee_profile") or (meta.get("validator_profiles") or [DEFAULT_VALIDATOR])[0],
                    parents=validation_parents,
                    priority=60,
                    workspace=meta.get("workspace", f"dir:{meta['repo']}"),
                )
                meta["kanban"].setdefault("validators", {})[vid] = task_id
                meta["kanban"].setdefault("task_ids", []).append(task_id)
                created.append(task_id)
        meta["kanban"]["task_ids"] = sorted(set(meta["kanban"].get("task_ids") or []))
        kb.recompute_ready(conn)
    save_mission(path, meta)
    summary = derive_status(meta)
    summary["created_validation_tasks"] = created
    _write_status(path, summary)
    return summary


def _latest_validation_report(path: Path, milestone_id: Optional[str] = None) -> Optional[Path]:
    val_dir = path / "validation"
    if not val_dir.exists():
        return None
    files = sorted(val_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if milestone_id:
        files = [p for p in files if p.name.startswith(f"{milestone_id}-")]
    return files[0] if files else None


def _extract_fix_titles(text: str) -> list[str]:
    titles: list[str] = []
    capture = False
    for raw in text.splitlines():
        line = raw.strip()
        lower = line.lower().strip(":")
        if "recommended fix task" in lower or "blocking_issues" in lower or "blocking issues" in lower:
            capture = True
            continue
        if capture and line.startswith(("#", "##")):
            capture = False
        if capture and line.startswith(("-", "*")):
            title = line[1:].strip().strip("-: ")
            if title:
                titles.append(title[:120])
    return titles[:20]


def retry_blockers(mission_id: str) -> dict[str, Any]:
    path, meta = load_mission(mission_id)
    report = _latest_validation_report(path)
    if not report:
        raise FileNotFoundError(f"no validation reports found under {path / 'validation'}")
    titles = _extract_fix_titles(report.read_text(encoding="utf-8"))
    if not titles:
        titles = [f"Fix blockers from {report.name}"]
    worker_profile = (meta.get("worker_profiles") or {}).get("backend") or next(iter((meta.get("worker_profiles") or {"worker": "worker"}).values()))
    created = []
    round_no = int(meta.get("validation_round") or 1) + 1
    with kb.connect(board=meta["board"]) as conn:
        parents = []
        gate_map = meta.get("kanban", {}).get("gates", {}) or {}
        if gate_map:
            parents.append(list(gate_map.values())[-1])
        for i, title in enumerate(titles, 1):
            fid = f"FIX-R{round_no}-{i:03d}"
            task_id = _create_or_get_task(
                conn, meta,
                key=f"fix:{fid}",
                title=f"{fid}: {title}",
                body=fix_task_body(meta, fid, title, report),
                assignee=worker_profile,
                parents=parents,
                priority=80,
                workspace=meta.get("workspace", f"dir:{meta['repo']}"),
            )
            meta["kanban"].setdefault("fixes", {})[fid] = task_id
            meta["kanban"].setdefault("task_ids", []).append(task_id)
            created.append(task_id)
        meta["kanban"]["task_ids"] = sorted(set(meta["kanban"].get("task_ids") or []))
        kb.recompute_ready(conn)
    meta["state"] = "running"
    save_mission(path, meta)
    summary = derive_status(meta)
    summary["created_fix_tasks"] = created
    summary["source_report"] = str(report)
    _write_status(path, summary)
    return summary


def fix_task_body(meta: dict[str, Any], fix_id: str, title: str, report: Path) -> str:
    return f"""Mission ID: {meta['mission_id']}
Fix Feature ID: {fix_id}
Repo: {meta['repo']}
Source validator report: {report}
Scope: {title}{_format_model_hint('Worker', _resolve_agent_model(meta.get('agent_models') or {}, 'workers', 'backend'))}

Instructions:
1. Read the source validator report.
2. Implement only the targeted fix for the blocking issue.
3. Add/update regression tests where practical.
4. Run relevant tests.
5. Complete with files_changed, tests_run, evidence, risks, and followups.
"""


def list_missions() -> list[dict[str, Any]]:
    idx = _load_index().get("missions", {})
    rows = list(idx.values())
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return rows


def doctor() -> dict[str, Any]:
    checks = []
    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        meta = ensure_missions_board(DEFAULT_BOARD)
        add("missions board", True, meta.get("db_path", ""))
    except Exception as exc:
        add("missions board", False, str(exc))

    try:
        from gateway.status import get_running_pid  # type: ignore
        pid = get_running_pid()
        add("gateway dispatcher", bool(pid), f"pid={pid}" if pid else "gateway not running; tasks will wait")
    except Exception as exc:
        add("gateway dispatcher", False, f"could not probe gateway: {exc}")

    try:
        root = kb.kanban_home()
        default_home = os.environ.get("HERMES_HOME", str(get_hermes_home()))
        add("kanban shared root", "profiles" not in str(kb.kanban_db_path(DEFAULT_BOARD).parent), f"root={root}, HERMES_HOME={default_home}")
    except Exception as exc:
        add("kanban shared root", False, str(exc))

    for profile in [DEFAULT_ORCHESTRATOR, DEFAULT_VALIDATOR, "backend-eng", "frontend-eng"]:
        p = get_hermes_home() / "profiles" / profile
        add(f"profile {profile}", p.exists(), str(p) if p.exists() else "recommended profile missing")

    try:
        from tools import kanban_tools as kt
        add("orchestrator kanban tool path", callable(getattr(kt, "_check_kanban_mode", None)), "kanban toolset or HERMES_KANBAN_TASK enables tools")
    except Exception as exc:
        add("orchestrator kanban tool path", False, str(exc))

    try:
        cfg_path = get_hermes_home() / "config.yaml"
        cfg_text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
        add("local model context", True, "check model.context_length; missions work best with >=32k context")
    except Exception as exc:
        add("local model context", False, str(exc))

    return {"ok": all(c["ok"] for c in checks if c["name"] in {"missions board", "kanban shared root", "orchestrator kanban tool path"}), "checks": checks}


def export_mission(mission_id: str, output: str) -> dict[str, Any]:
    path, meta = load_mission(mission_id)
    out = Path(output).expanduser().resolve()
    if out.suffix.lower() == ".zip":
        base = out.with_suffix("")
        if out.exists():
            out.unlink()
        archive = shutil.make_archive(str(base), "zip", root_dir=path.parent, base_dir=path.name)
        if Path(archive) != out:
            Path(archive).rename(out)
    else:
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(path, out)
    return {"mission_id": mission_id, "output": str(out)}


# ---------------------------------------------------------------------------
# Structured validation report schema
# ---------------------------------------------------------------------------

def validation_report_template(mission_id: str, milestone_id: str, round_no: int) -> str:
    """Return a structured validation report template with YAML frontmatter."""
    return f"""# Validation Report: {mission_id} — {milestone_id} round {round_no}

```yaml
passed_assertions: []
failed_assertions: []
blocking_issues: []
non_blocking_issues: []
suggestions: []
evidence:
  - command: pytest tests/
    result: passed
    output: ""
reproduction_steps: []
recommended_fix_task_titles: []
```

## Narrative

Write your validation findings here. Reference assertions by VAL-<AREA>-<NNN>.

## Evidence

Record exact commands, test output, and file changes inspected.

"""


def parse_validation_report(report_path: Path) -> dict[str, Any]:
    """Parse a validation report and extract the structured YAML frontmatter.

    Falls back to heuristic Markdown parsing if YAML frontmatter is missing.
    """
    text = report_path.read_text(encoding="utf-8")

    # Try to extract YAML frontmatter
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                data = yaml.safe_load(parts[1])
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

    # Fallback: heuristic Markdown parsing
    result: dict[str, Any] = {
        "passed_assertions": [],
        "failed_assertions": [],
        "blocking_issues": [],
        "non_blocking_issues": [],
        "evidence": [],
        "recommended_fix_task_titles": [],
    }
    capture = None
    for raw in text.splitlines():
        line = raw.strip()
        lower = line.lower().strip(":")
        if "passed assertion" in lower:
            capture = "passed_assertions"
            continue
        elif "failed assertion" in lower:
            capture = "failed_assertions"
            continue
        elif "blocking issue" in lower:
            capture = "blocking_issues"
            continue
        elif "non blocking issue" in lower or "non-blocking issue" in lower:
            capture = "non_blocking_issues"
            continue
        elif "recommended fix" in lower:
            capture = "recommended_fix_task_titles"
            continue
        if capture and line.startswith(("#", "##")):
            capture = None
        if capture and line.startswith(("-", "*")):
            item = line[1:].strip().strip("-: ")
            if item:
                result[capture].append(item)
    return result


# ---------------------------------------------------------------------------
# Assertion ledger
# ---------------------------------------------------------------------------

def _ledger_path(path: Path) -> Path:
    return path / "validation" / "assertion-ledger.json"


def _load_ledger(path: Path) -> dict[str, Any]:
    p = _ledger_path(path)
    if not p.exists():
        return {"assertions": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"assertions": {}}


def _save_ledger(path: Path, data: dict[str, Any]) -> None:
    p = _ledger_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def update_ledger_from_report(mission_id: str, report_path: Path) -> dict[str, Any]:
    """Update the assertion ledger from a validation report.

    Returns the ledger with updated assertion states.
    """
    path, meta = load_mission(mission_id)
    report = parse_validation_report(report_path)
    ledger = _load_ledger(path)
    assertions = ledger.setdefault("assertions", {})

    round_no = int(meta.get("validation_round") or 1)
    report_name = report_path.name

    # Update passed assertions
    for a in report.get("passed_assertions") or []:
        aid = a.get("id", str(a)) if isinstance(a, dict) else (a.split(":")[0].strip() if ":" in a else a.strip())
        if aid in assertions:
            assertions[aid]["latest_round"] = round_no
            assertions[aid]["status"] = "passed"
            assertions[aid]["evidence"] = report.get("evidence", [])
        else:
            assertions[aid] = {
                "latest_round": round_no,
                "status": "passed",
                "evidence": report.get("evidence", []),
                "blocking": False,
            }

    # Update failed assertions
    for a in report.get("failed_assertions") or []:
        aid = a.get("id", str(a)) if isinstance(a, dict) else (a.split(":")[0].strip() if ":" in a else a.strip())
        if aid in assertions:
            assertions[aid]["latest_round"] = round_no
            assertions[aid]["status"] = "failed"
            assertions[aid]["blocking_issues"] = report.get("blocking_issues", [])
        else:
            assertions[aid] = {
                "latest_round": round_no,
                "status": "failed",
                "blocking_issues": report.get("blocking_issues", []),
                "blocking": False,
            }

    ledger["assertions"] = assertions
    ledger["updated_at"] = _now_iso()
    _save_ledger(path, ledger)
    return ledger


def check_mission_passed(mission_id: str) -> dict[str, Any]:
    """Check if all blocking assertions in the ledger have passed.

    Returns a dict with passed, failed, and blocking_failed assertions.
    """
    path, meta = load_mission(mission_id)
    ledger = _load_ledger(path)
    assertions = ledger.get("assertions", {})

    passed = []
    failed = []
    blocking_failed = []

    for aid, info in assertions.items():
        if info.get("status") == "passed":
            passed.append(aid)
        elif info.get("status") == "failed":
            failed.append(aid)
            if info.get("blocking"):
                blocking_failed.append(aid)

    result = {
        "mission_id": mission_id,
        "passed": passed,
        "failed": failed,
        "blocking_failed": blocking_failed,
        "all_passed": len(failed) == 0 and len(passed) > 0,
        "no_blocking_failures": len(blocking_failed) == 0,
    }
    return result


def mark_mission_passed(mission_id: str) -> dict[str, Any]:
    """Mark mission as passed if all blocking assertions are satisfied."""
    check = check_mission_passed(mission_id)
    if not check["no_blocking_failures"]:
        raise ValueError(
            f"cannot mark passed: {len(check['blocking_failed'])} blocking assertions failed: "
            f"{check['blocking_failed']}"
        )
    path, meta = load_mission(mission_id)
    meta["state"] = "passed"
    save_mission(path, meta)
    return derive_status(meta)


def _mission_profiles_result(
    install: bool = False,
    *,
    orchestrator_model: Optional[str] = None,
    orchestrator_provider: Optional[str] = None,
    worker_model: Optional[str] = None,
    worker_provider: Optional[str] = None,
    validator_model: Optional[str] = None,
    validator_provider: Optional[str] = None,
) -> dict[str, Any]:
    """Return recommended profile setup commands for Missions."""
    profiles = [
        "mission-orchestrator",
        "backend-eng",
        "frontend-eng",
        "flutter-eng",
        "qa-validator",
        "user-tester",
        "release-manager",
    ]
    commands = [f"hermes profile create {p} --clone" for p in profiles]
    commands.append("hermes -p mission-orchestrator config set toolsets '[\"kanban\"]'")

    def add_model_commands(profile: str, model: Optional[str], provider: Optional[str]) -> None:
        if model:
            commands.append(f"hermes -p {profile} config set model.default {model}")
        if provider:
            commands.append(f"hermes -p {profile} config set model.provider {provider}")

    add_model_commands("mission-orchestrator", orchestrator_model, orchestrator_provider)
    for profile in ["worker", "backend-eng", "frontend-eng", "flutter-eng"]:
        add_model_commands(profile, worker_model, worker_provider)
    for profile in ["validator", "qa-validator", "user-tester"]:
        add_model_commands(profile, validator_model, validator_provider)

    return {
        "profiles": profiles,
        "commands": commands,
        "install": install,
        "models": {
            "orchestrator": _agent_model_entry(orchestrator_model, orchestrator_provider),
            "workers": _agent_model_entry(worker_model, worker_provider),
            "validators": _agent_model_entry(validator_model, validator_provider),
        },
        "note": "Kanban dispatch uses the assignee's Hermes profile. Configure model.default and model.provider per profile to run different Mission roles on different providers.",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def setup_cli(parser: argparse.ArgumentParser) -> None:
    """Populate the plugin-provided `hermes mission` argparse tree."""
    sub = parser.add_subparsers(dest="mission_action")

    p_init = sub.add_parser("init", help="Create the missions Kanban board if missing")
    p_init.add_argument("--board", default=DEFAULT_BOARD)
    p_init.add_argument("--json", action="store_true")

    p_create = sub.add_parser("create", help="Create a new mission")
    p_create.add_argument("goal")
    p_create.add_argument("--board", default=DEFAULT_BOARD)
    p_create.add_argument("--repo")
    p_create.add_argument("--assignee", default=DEFAULT_ORCHESTRATOR)
    p_create.add_argument("--validator", action="append", default=[])
    p_create.add_argument("--worker", action="append", default=[])
    p_create.add_argument("--workspace", default=None)
    p_create.add_argument("--tenant")
    p_create.add_argument("--priority", type=int, default=50)
    p_create.add_argument("--idempotency-key")
    p_create.add_argument("--skip-clarification", action="store_true")
    p_create.add_argument("--orchestrator-model", help="Model to record for orchestrator tasks; configure the orchestrator profile to use it at runtime")
    p_create.add_argument("--orchestrator-provider", help="Provider to record for orchestrator tasks")
    p_create.add_argument("--worker-model", action="append", default=[], help="Worker model mapping: MODEL for default or role=MODEL, repeatable")
    p_create.add_argument("--worker-provider", action="append", default=[], help="Worker provider mapping: PROVIDER for default or role=PROVIDER, repeatable")
    p_create.add_argument("--validator-model", action="append", default=[], help="Validator model mapping: MODEL for default or validator-id=MODEL, repeatable")
    p_create.add_argument("--validator-provider", action="append", default=[], help="Validator provider mapping: PROVIDER for default or validator-id=PROVIDER, repeatable")
    p_create.add_argument("--dry-run", action="store_true")
    p_create.add_argument("--json", action="store_true")

    for name in ["plan", "approve", "start", "retry-blockers", "unblock", "archive"]:
        p = sub.add_parser(name, help=f"{name} a mission")
        p.add_argument("mission_id")
        p.add_argument("--json", action="store_true")

    p_status = sub.add_parser("status", help="Summarize mission state")
    p_status.add_argument("mission_id")
    p_status.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="Show mission metadata and artifacts")
    p_show.add_argument("mission_id")
    p_show.add_argument("--json", action="store_true")

    p_validate = sub.add_parser("validate", help="Create validation tasks")
    p_validate.add_argument("mission_id")
    p_validate.add_argument("--milestone")
    p_validate.add_argument("--json", action="store_true")

    p_block = sub.add_parser("block", help="Block a mission with a reason")
    p_block.add_argument("mission_id")
    p_block.add_argument("reason")
    p_block.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="List indexed missions")
    p_list.add_argument("--json", action="store_true")

    p_doctor = sub.add_parser("doctor", help="Check Missions/Kanban readiness")
    p_doctor.add_argument("--json", action="store_true")

    p_export = sub.add_parser("export", help="Export mission artifacts")
    p_export.add_argument("mission_id")
    p_export.add_argument("--output", required=True)
    p_export.add_argument("--json", action="store_true")

    p_check = sub.add_parser("check", help="Check assertion ledger and mission pass status")
    p_check.add_argument("mission_id")
    p_check.add_argument("--json", action="store_true")

    p_mark_pass = sub.add_parser("mark-passed", help="Mark mission passed if all blocking assertions satisfied")
    p_mark_pass.add_argument("mission_id")
    p_mark_pass.add_argument("--json", action="store_true")

    p_profiles = sub.add_parser("profiles", help="Show recommended profile setup commands")
    p_profiles.add_argument("--install", action="store_true", help="Print commands to create recommended profiles")
    p_profiles.add_argument("--orchestrator-model", help="Model for mission-orchestrator profile")
    p_profiles.add_argument("--orchestrator-provider", help="Provider for mission-orchestrator profile")
    p_profiles.add_argument("--worker-model", help="Model for worker profiles")
    p_profiles.add_argument("--worker-provider", help="Provider for worker profiles")
    p_profiles.add_argument("--validator-model", help="Model for validator profiles")
    p_profiles.add_argument("--validator-provider", help="Provider for validator profiles")
    p_profiles.add_argument("--json", action="store_true")

    parser.set_defaults(func=mission_command)


def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Backward-compatible parser builder for direct/core imports."""
    parser = parent_subparsers.add_parser("mission", help="Factory-style Missions on Hermes Kanban")
    setup_cli(parser)
    return parser


def mission_command(args: argparse.Namespace) -> int:
    action = getattr(args, "mission_action", None)
    if not action:
        print("usage: hermes mission <action> [options]", file=sys.stderr)
        return 2
    try:
        if action == "init":
            board = getattr(args, "board", DEFAULT_BOARD)
            meta = ensure_missions_board(board, switch=True)
            return _emit(meta, getattr(args, "json", False), f"Missions board ready: {meta.get('slug', board)}")
        if action == "create":
            res = create_mission(
                args.goal,
                repo_arg=args.repo,
                board=args.board,
                orchestrator=args.assignee,
                validators=args.validator or [DEFAULT_VALIDATOR],
                workers=args.worker,
                workspace=args.workspace,
                tenant=args.tenant,
                priority=args.priority,
                idempotency_key=args.idempotency_key,
                skip_clarification=args.skip_clarification,
                dry_run=args.dry_run,
                orchestrator_model=args.orchestrator_model,
                orchestrator_provider=args.orchestrator_provider,
                worker_models=args.worker_model,
                worker_providers=args.worker_provider,
                validator_models=args.validator_model,
                validator_providers=args.validator_provider,
            )
            payload = {"mission_id": res.mission_id, "path": str(res.path), "dry_run": args.dry_run, "root_task": res.meta.get("root_task_id"), "artifacts": res.meta.get("artifacts"), "agent_models": res.meta.get("agent_models", {})}
            if args.dry_run:
                payload["planned_root_task"] = {"title": f"MISSION: {res.meta['title']}", "assignee": res.meta["orchestrator_profile"], "board": res.meta["board"]}
            return _emit(payload, args.json, f"Mission {'dry run' if args.dry_run else 'created'}: {res.mission_id}\nPath: {res.path}\nRoot task: {payload.get('root_task') or '(dry-run)'}")
        if action == "plan":
            return _emit(plan_mission(args.mission_id), args.json)
        if action == "approve":
            return _emit(approve_mission(args.mission_id), args.json)
        if action == "start":
            return _emit(start_mission(args.mission_id), args.json)
        if action == "status":
            path, meta = load_mission(args.mission_id)
            summary = derive_status(meta)
            _write_status(path, summary)
            return _emit(summary, args.json, _format_status(summary))
        if action == "show":
            path, meta = load_mission(args.mission_id)
            payload = {"path": str(path), "mission": meta, "status": derive_status(meta)}
            return _emit(payload, args.json, _format_show(path, meta))
        if action == "validate":
            return _emit(validate_mission(args.mission_id, args.milestone), args.json)
        if action == "retry-blockers":
            return _emit(retry_blockers(args.mission_id), args.json)
        if action == "block":
            return _emit(block_mission(args.mission_id, args.reason), args.json)
        if action == "unblock":
            return _emit(unblock_mission(args.mission_id), args.json)
        if action == "list":
            rows = list_missions()
            if args.json:
                return _emit(rows, True)
            if not rows:
                print("(no indexed missions)")
            else:
                for r in rows:
                    print(f"{r.get('mission_id')}  {r.get('state')}  {r.get('repo')}  {r.get('title')}")
            return 0
        if action == "archive":
            return _emit(archive_mission(args.mission_id), args.json)
        if action == "doctor":
            result = doctor()
            return _emit(result, getattr(args, "json", False), _format_doctor(result))
        if action == "export":
            return _emit(export_mission(args.mission_id, args.output), args.json)
        if action == "check":
            return _emit(check_mission_passed(args.mission_id), args.json)
        if action == "mark-passed":
            return _emit(mark_mission_passed(args.mission_id), args.json)
        if action == "profiles":
            return _emit(
                _mission_profiles_result(
                    getattr(args, "install", False),
                    orchestrator_model=args.orchestrator_model,
                    orchestrator_provider=args.orchestrator_provider,
                    worker_model=args.worker_model,
                    worker_provider=args.worker_provider,
                    validator_model=args.validator_model,
                    validator_provider=args.validator_provider,
                ),
                args.json,
            )
    except Exception as exc:
        print(f"mission: {exc}", file=sys.stderr)
        return 1
    print(f"unknown mission action: {action}", file=sys.stderr)
    return 2


def _emit(payload: Any, as_json: bool, text: Optional[str] = None) -> int:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(text if text is not None else json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _format_status(s: dict[str, Any]) -> str:
    lines = [
        f"Mission: {s.get('mission_id')} ({s.get('state')})",
        f"Board: {s.get('board')}  Root: {s.get('root_task_id')}",
        f"Current milestone: {s.get('current_milestone') or '-'}  Validation round: {s.get('validation_round')}",
        f"Tasks: {s.get('task_counts')}",
        f"Features: {s.get('feature_progress')}",
        f"Validators: {s.get('validator_progress')}",
    ]
    blockers = s.get("blockers") or []
    if blockers:
        lines.append("Blockers:")
        lines.extend(f"- {b['id']} {b['title']} ({b.get('assignee') or '-'})" for b in blockers)
    ready = s.get("next_ready_tasks") or []
    if ready:
        lines.append("Next ready tasks:")
        lines.extend(f"- {r['id']} {r['title']} ({r.get('assignee') or '-'})" for r in ready[:10])
    return "\n".join(lines)


def _format_show(path: Path, meta: dict[str, Any]) -> str:
    lines = [
        f"Mission {meta.get('mission_id')}: {meta.get('title')}",
        f"State: {meta.get('state')}",
        f"Repo: {meta.get('repo')}",
        f"Board: {meta.get('board')}",
        f"Root task: {meta.get('root_task_id')}",
        f"Path: {path}",
        "Artifacts:",
    ]
    lines.extend(f"- {k}: {v}" for k, v in (meta.get("artifacts") or {}).items())
    return "\n".join(lines)


def _format_doctor(result: dict[str, Any]) -> str:
    lines = [f"Missions doctor: {'OK' if result.get('ok') else 'ISSUES'}"]
    for c in result.get("checks", []):
        lines.append(f"{'✓' if c.get('ok') else '✗'} {c.get('name')}: {c.get('detail')}")
    return "\n".join(lines)
