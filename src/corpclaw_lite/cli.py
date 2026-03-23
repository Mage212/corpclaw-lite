"""
CorpClaw Lite — CLI entrypoint.

All commands are auto-discovered from this module by pyproject.toml:

    [project.scripts]
    corpclaw-lite = "corpclaw_lite.cli:main"

Usage:
    uv run corpclaw-lite chat
    uv run corpclaw-lite telegram
    uv run corpclaw-lite user-list
    uv run corpclaw-lite user-create -t <telegram_id> -d <department>
    uv run corpclaw-lite skill list
    uv run corpclaw-lite plugin list
    uv run corpclaw-lite containers
    uv run corpclaw-lite prune
    uv run corpclaw-lite generate skill <name>
    uv run corpclaw-lite generate plugin <name>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"Error: environment variable {name!r} is required but not set.", file=sys.stderr)
        sys.exit(1)
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpclaw-lite",
        description="CorpClaw Lite — Corporate AI Agent",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # chat
    sub.add_parser("chat", help="Start an interactive CLI chat session")

    # telegram
    sub.add_parser("telegram", help="Start the Telegram bot (polling)")

    # user
    sub.add_parser("user-list", help="List all registered users")

    create_p = sub.add_parser("user-create", help="Create a new user")
    create_p.add_argument("-t", "--telegram-id", type=int, required=True, help="Telegram user ID")
    create_p.add_argument("-d", "--department", required=True, help="Department slug")
    create_p.add_argument("-n", "--name", default="", help="Display name")

    # containers
    sub.add_parser("containers", help="List active Docker containers")
    sub.add_parser("prune", help="Remove idle Docker containers")

    # skill / plugin
    skill_p = sub.add_parser("skill", help="Skill management")
    skill_p.add_argument("action", choices=["list"], help="Action to perform")

    plugin_p = sub.add_parser("plugin", help="Plugin management")
    plugin_p.add_argument("action", choices=["list"], help="Action to perform")

    # generate
    gen_p = sub.add_parser("generate", help="Generate extension scaffolding")
    gen_p.add_argument("type", choices=["skill", "plugin", "subagent"])
    gen_p.add_argument("name", help="Name of the new extension")

    return parser


# ──────────────────────────────────────────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────────────────────────────────────────


def cmd_chat() -> None:
    """Launch an interactive CLI chat loop."""
    import asyncio

    from corpclaw_lite.logging.agent_logger import setup_logging

    setup_logging()

    async def _run() -> None:
        from corpclaw_lite.channels.cli import CLIChannel
        from corpclaw_lite.users.models import User

        # Minimal bootstrap: create a local CLI user
        user = User(id=0, name="cli-user", department="default", telegram_id=None)
        channel = CLIChannel()
        await channel.start()
        print("CorpClaw Lite – CLI chat (Ctrl+C to quit)")
        try:
            while True:
                msg = await asyncio.get_event_loop().run_in_executor(None, lambda: input("You: "))
                if msg.strip():
                    await channel.send_message(user, f"[echo] {msg}")
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            await channel.stop()

    asyncio.run(_run())


def cmd_telegram() -> None:
    """Start the Telegram bot in polling mode."""
    token = _require_env("TELEGRAM_BOT_TOKEN")
    _require_env("CORPCLAW_IPC_SECRET")  # fail fast

    from corpclaw_lite.logging.agent_logger import setup_logging

    setup_logging()

    from corpclaw_lite.channels.telegram.runner import run_telegram_bot

    asyncio.run(run_telegram_bot(token))


def cmd_user_list() -> None:
    """Print all registered users from the SQLite DB."""
    from corpclaw_lite.users.manager import UserManager

    manager = UserManager()
    users = manager.list_users()
    if not users:
        print("No users registered.")
        return
    print(f"{'ID':<6} {'Telegram ID':<14} {'Name':<20} {'Department'}")
    print("-" * 56)
    for u in users:
        print(f"{u.id:<6} {str(u.telegram_id):<14} {u.name:<20} {u.department}")


def cmd_user_create(telegram_id: int, department: str, name: str) -> None:
    """Register a new user."""
    from corpclaw_lite.users.manager import UserManager

    manager = UserManager()
    user = manager.create_user(telegram_id=telegram_id, department=department, name=name)
    print(f"Created user #{user.id}: {user.name} ({user.department})")


def cmd_containers() -> None:
    """List active Docker sandbox containers."""
    import asyncio

    from corpclaw_lite.container.manager import ContainerManager

    mgr = ContainerManager()
    containers = asyncio.run(mgr.list_active())
    if not containers:
        print("No active containers.")
        return
    for c in containers:
        print(c)


def cmd_prune() -> None:
    """Stop and remove idle containers."""
    import asyncio

    from corpclaw_lite.container.manager import ContainerManager

    mgr = ContainerManager()
    removed = asyncio.run(mgr.prune_idle())
    print(f"Pruned {removed} container(s).")


def cmd_skill_list() -> None:
    """List all loaded skills from the skills/ directory."""
    from corpclaw_lite.extensions.skills.registry import SkillRegistry

    registry = SkillRegistry()
    registry.load_directory("skills")
    skills = registry.list_all()
    if not skills:
        print("No skills found in skills/")
        return
    print(f"{'ID':<30} {'Version':<10} {'Departments'}")
    print("-" * 60)
    for s in skills:
        depts = ", ".join(s.allowed_for)
        print(f"{s.id:<30} {s.version:<10} {depts}")


def cmd_plugin_list() -> None:
    """List all loaded plugins from the plugins/ directory."""
    from corpclaw_lite.extensions.plugins.registry import PluginRegistry

    registry = PluginRegistry()
    registry.load_directory("plugins")
    plugins = registry.list_all()
    if not plugins:
        print("No plugins found in plugins/")
        return
    print(f"{'Name':<30} {'Version':<10} {'Description'}")
    print("-" * 70)
    for p in plugins:
        print(f"{p.manifest.name:<30} {p.manifest.version:<10} {p.manifest.description[:28]}")


_SKILL_TEMPLATE = """\
---
id: {name}
description: Short description of what this skill teaches the agent
version: "1.0.0"
allowed_for:
  - "*"   # or specific departments: [marketing, hr]
---

# {title} Skill

## Context

Describe when and why the agent should use this skill.

## Instructions

1. Step one
2. Step two
3. Step three

## Examples

**Input:** User asks…
**Output:** Agent does…
"""

_PLUGIN_MANIFEST_TEMPLATE = """\
name: {name}
version: "1.0.0"
type: plugin
description: Short description of {name}
allowed_departments:
  - "*"
components:
  skill: skill.md
  # tool: tool.py
  # script: scripts/run.sh
"""

_PLUGIN_SKILL_TEMPLATE = """\
---
id: {name}
description: {name} plugin skill
version: "1.0.0"
allowed_for:
  - "*"
---

# {title} Plugin

## Instructions

Describe what this plugin does and how the agent should use it.
"""

_SUBAGENT_TEMPLATE = """\
id: {name}
description: Short description of what this subagent specialises in
capabilities:
  - capability_one
  - capability_two
allowed_tools:
  - read_file
  - write_file
  - list_files
prompt_path: config/bootstrap/SOUL.md
"""


def cmd_generate(ext_type: str, name: str) -> None:
    """Scaffold a new skill, plugin, or subagent."""
    from pathlib import Path

    title = name.replace("_", " ").replace("-", " ").title()

    if ext_type == "skill":
        path = Path("skills") / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_SKILL_TEMPLATE.format(name=name, title=title), encoding="utf-8")
        print(f"Created skill: {path}")

    elif ext_type == "plugin":
        base = Path("plugins") / name
        base.mkdir(parents=True, exist_ok=True)
        (base / "manifest.yaml").write_text(
            _PLUGIN_MANIFEST_TEMPLATE.format(name=name), encoding="utf-8"
        )
        (base / "skill.md").write_text(
            _PLUGIN_SKILL_TEMPLATE.format(name=name, title=title), encoding="utf-8"
        )
        print(f"Created plugin: {base}/")

    elif ext_type == "subagent":
        path = Path("config") / "subagents" / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_SUBAGENT_TEMPLATE.format(name=name), encoding="utf-8")
        print(f"Created subagent spec: {path}")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "chat":
        cmd_chat()
    elif args.command == "telegram":
        cmd_telegram()
    elif args.command == "user-list":
        cmd_user_list()
    elif args.command == "user-create":
        cmd_user_create(args.telegram_id, args.department, args.name)
    elif args.command == "containers":
        cmd_containers()
    elif args.command == "prune":
        cmd_prune()
    elif args.command == "skill" and args.action == "list":
        cmd_skill_list()
    elif args.command == "plugin" and args.action == "list":
        cmd_plugin_list()
    elif args.command == "generate":
        cmd_generate(args.type, args.name)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
