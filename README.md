# Hermes Missions Plugin

Factory-style long-running project workflows for Hermes Agent, built on Hermes Kanban.

This repository is a simple Hermes directory plugin. It is meant to be installed with `hermes plugins install`, not as a pip package.

## What it provides

After installation and enablement, the plugin registers the top-level CLI command:

```bash
hermes mission --help
```

The command exposes Missions workflows such as:

```text
init, create, plan, approve, start, retry-blockers, unblock, archive,
status, show, validate, block, list, doctor, export, check,
mark-passed, profiles
```

## Install

From GitHub, after this repo is published:

```bash
hermes plugins install <owner>/hermes-plugin-missions --enable
```

From an explicit Git URL:

```bash
hermes plugins install https://github.com/<owner>/hermes-plugin-missions.git --enable
```

For local smoke testing from this checkout, use a `file://` URL:

```bash
hermes plugins install file:///home/max/Projects/hermes-plugin-missions --enable
```

Hermes installs directory plugins into:

```text
$HERMES_HOME/plugins/missions/
```

and enables them by adding `missions` to:

```yaml
plugins:
  enabled:
    - missions
```

## Requirements

- Hermes Agent with general plugin CLI command discovery support.
- Hermes Kanban internals available from the active Hermes installation.

This plugin intentionally imports Hermes internals such as `hermes_cli.kanban_db` and `hermes_constants`. It is standalone as a plugin repository, not standalone as a generic Python package.

## Development

The plugin root must contain:

```text
plugin.yaml
__init__.py
missions.py
```

`__init__.py` registers the CLI command through the Hermes Plugin SDK:

```python
ctx.register_cli_command(
    name="mission",
    help="Factory-style Missions on Hermes Kanban",
    setup_fn=setup_cli,
    handler_fn=mission_command,
)
```

Run syntax checks:

```bash
python3 -m py_compile __init__.py missions.py
```

Run tests from this repo with the Hermes source checkout available at `/home/max/.hermes/hermes-agent`:

```bash
cd /home/max/Projects/hermes-plugin-missions
PYTHONPATH=/home/max/.hermes/hermes-agent:. /home/max/.hermes/hermes-agent/venv/bin/python3 -m pytest tests -q -o 'addopts='
```

Or override the Hermes checkout path:

```bash
HERMES_AGENT_REPO=/path/to/hermes-agent \
PYTHONPATH=/path/to/hermes-agent:. \
/path/to/hermes-agent/venv/bin/python3 -m pytest tests -q -o 'addopts='
```

## Notes

The Hermes core compatibility wrapper may continue to live in Hermes itself as `hermes_cli/missions.py` for old imports. This plugin repository should be treated as the canonical plugin implementation.
