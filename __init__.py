"""Hermes Missions plugin.

Factory-style long-running project workflows built on Hermes Kanban.
"""
from __future__ import annotations


def register(ctx):
    """Register the `hermes mission` CLI command via the plugin SDK."""
    from .missions import setup_cli, mission_command

    ctx.register_cli_command(
        name="mission",
        help="Factory-style Missions on Hermes Kanban",
        description="Factory-style long-running project workflows built on Hermes Kanban.",
        setup_fn=setup_cli,
        handler_fn=mission_command,
    )
