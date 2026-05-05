---
sidebar_position: 13
title: "Missions"
description: "Factory-style long-running project workflows built on Hermes Kanban"
---

# Missions

Missions are a thin Factory-style project workflow layer on top of Hermes Kanban. A Mission turns a large goal into durable repo-local artifacts plus a linked Kanban task graph:

- Mission = one `missions` board root task plus files under `.missions/<mission-id>/`
- Milestone = parent/group Kanban task
- Feature = bounded implementation Kanban task assigned to a worker profile
- Validator = independent Kanban task assigned to a validator profile
- Fix feature = Kanban task generated from blocking validator findings

Missions intentionally do not create an in-process subagent swarm. The gateway-embedded Kanban dispatcher remains the execution engine, so workers are normal named Hermes profiles with fresh context, their own SOUL.md identity, durable comments, task links, runs, and workspaces.

## Quick start

```bash
# Ensure the board exists.
hermes mission init

# Preview without writing files or Kanban rows.
hermes mission create "Add OAuth login with tests" \
  --repo /absolute/path/to/repo \
  --dry-run

# Create mission artifacts and the root Kanban task.
hermes mission create "Add OAuth login with tests" \
  --repo /absolute/path/to/repo \
  --worker backend=backend-eng \
  --validator qa-validator

# Ask the orchestrator to prepare/update the validation contract and feature graph.
hermes mission plan MISSION-YYYYMMDD-HHMMSS-add-oauth-login-with-tests

# Approve and start the linked Kanban graph.
hermes mission approve MISSION-...
hermes mission start MISSION-...

# Track progress.
hermes mission status MISSION-...
hermes kanban --board missions list
```

## CLI reference

```bash
hermes mission init
hermes mission create "<goal>" [options]
hermes mission plan <mission-id>
hermes mission approve <mission-id>
hermes mission start <mission-id>
hermes mission status <mission-id> [--json]
hermes mission show <mission-id> [--json]
hermes mission validate <mission-id> [--milestone <id>]
hermes mission retry-blockers <mission-id>
hermes mission block <mission-id> "<reason>"
hermes mission unblock <mission-id>
hermes mission list [--json]
hermes mission archive <mission-id>
hermes mission doctor [--json]
hermes mission export <mission-id> --output <path>
```

`create` options:

- `--board <slug>`: Kanban board slug. Default: `missions`.
- `--repo <absolute-path>`: target repository. If omitted, Hermes searches upward from cwd for `.git`, `pyproject.toml`, or `package.json`.
- `--assignee <profile>`: orchestrator profile. Default: `mission-orchestrator`.
- `--validator <profile>`: repeatable validator profile. Default: `validator`.
- `--worker <role=profile>`: repeatable role mapping, for example `backend=backend-eng` or `flutter=flutter-eng`.
- `--workspace scratch|worktree|dir:<path>`: default `dir:<repo>`.
- `--tenant <name>`: Kanban tenant. Default: mission id.
- `--priority <int>`: root task priority. Default: `50`.
- `--idempotency-key <key>`: stable mission id/key for repeatable automation.
- `--skip-clarification`: record that the orchestrator may skip clarification.
- `--dry-run`: print planned paths/task shape without modifying state.
- `--json`: machine-readable output.

## Artifact layout

For `/path/to/repo`, a mission creates:

```text
/path/to/repo/.missions/
  MISSION-YYYYMMDD-HHMMSS-slug/
    mission.yaml
    mission.md
    validation-contract.md
    features.json
    milestones.yaml
    services.yaml
    AGENTS.md
    knowledge.md
    status.json
    validation/
      <milestone-id>-round-<n>.md
    handoffs/
      worker-results.jsonl
      validator-results.jsonl
```

Kanban is the source of execution truth. `status.json` is only a cached summary for scripts and humans.

## Lifecycle

1. `create` writes template artifacts and creates a root `MISSION: ...` Kanban task.
2. `plan` creates an orchestrator planning task and writes/updates starter `validation-contract.md`, `features.json`, and `milestones.yaml`.
3. `approve` marks the mission approved.
4. `start` converts `features.json` into idempotent Kanban milestone, feature, validator, and gate tasks.
5. Feature tasks become ready only after parent/grouping dependencies are done.
6. Validator tasks depend on all feature tasks for that milestone.
7. Gate tasks depend on validator tasks and are assigned to the orchestrator.
8. `retry-blockers` reads the latest validation report and creates targeted fix tasks.
9. `validate` creates another validation round; when fix tasks exist, the new validators depend on those fix tasks as well as the original features.
10. Mission completion is only valid when all blocking assertions pass with evidence.

## Validation contract

`validation-contract.md` is the mission's testable definition of done:

```markdown
# Validation Contract: <mission title>

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
```

Validators must not fix code. They write validation reports under `validation/` and complete their Kanban tasks with pass/fail evidence.

## Recommended profiles

Create profiles whose SOUL.md matches their role. Example commands:

```bash
hermes profile create mission-orchestrator --clone
hermes profile create backend-eng --clone
hermes profile create frontend-eng --clone
hermes profile create flutter-eng --clone
hermes profile create qa-validator --clone

# Orchestrators that create/link tasks should enable the kanban toolset.
hermes -p mission-orchestrator config set toolsets '["kanban"]'
```

Note: if editing YAML directly, ensure `toolsets` is a YAML list, not a string.

Example profile identities:

```text
# mission-orchestrator SOUL.md
You are a mission orchestrator. You clarify requirements, write validation contracts, decompose work into bounded Kanban tasks, review validator findings, and create fix tasks. You do not directly implement large features.
```

```text
# worker SOUL.md
You are an implementation worker. You implement exactly one assigned feature, respect scope boundaries, write or update tests where practical, run relevant checks, and complete with structured evidence.
```

```text
# qa-validator SOUL.md
You are an independent validator. You validate assertions with evidence and report blocking/non-blocking issues. You do not implement fixes.
```

## Doctor checks

`hermes mission doctor` checks the missions board, gateway/dispatcher, shared Kanban root, recommended profiles, kanban tool visibility, and local model context suitability. If the gateway is not running, tasks can still be created but will not dispatch until the gateway dispatcher is started/configured.

## Troubleshooting

- Tasks are created but not running: check `hermes gateway status` and `hermes mission doctor`.
- Worker profile cannot see tasks: verify the Kanban DB path is shared across profiles and no `HERMES_KANBAN_DB` override points at a profile-local DB.
- Orchestrator cannot call `kanban_create`: enable the `kanban` toolset for that profile.
- Validators start fixing code: tighten validator SOUL.md. Kanban prompt guidance explicitly preserves SOUL.md identity; validator profiles should say “do not implement fixes”.
- Local model struggles with giant goals: keep milestones small and use sequential/low-concurrency execution.
