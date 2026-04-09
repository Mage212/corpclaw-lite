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
import concurrent.futures
import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

__all__ = [
    "cmd_calibrate",
    "cmd_chat",
    "cmd_containers",
    "cmd_generate",
    "cmd_plugin_list",
    "cmd_prune",
    "cmd_skill_list",
    "cmd_telegram",
    "cmd_user_allow",
    "cmd_user_create",
    "cmd_user_deny",
    "cmd_user_list",
    "cmd_user_revoke",
    "main",
]

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
    chat_p = sub.add_parser("chat", help="Start an interactive CLI chat session")
    chat_p.add_argument(
        "--telegram-id",
        type=int,
        required=True,
        help="Telegram ID пользователя (должен быть в БД, user-create)",
    )
    chat_p.add_argument(
        "--setup",
        action="store_true",
        help="Запустить/перезапустить настройку общения",
    )

    # telegram
    sub.add_parser("telegram", help="Start the Telegram bot (polling)")

    # user
    sub.add_parser("user-list", help="List all registered users")

    create_p = sub.add_parser("user-create", help="Create a new user")
    create_p.add_argument("-t", "--telegram-id", type=int, required=True, help="Telegram user ID")
    create_p.add_argument("-d", "--department", required=True, help="Department slug")
    create_p.add_argument("-n", "--name", default="", help="Display name")

    allow_p = sub.add_parser("user-allow", help="Add telegram_id to bot whitelist")
    allow_p.add_argument("-t", "--telegram-id", type=int, required=True, help="Telegram user ID")
    allow_p.add_argument("-d", "--department", default="default", help="Department slug")

    deny_p = sub.add_parser("user-deny", help="Remove telegram_id from bot whitelist")
    deny_p.add_argument("-t", "--telegram-id", type=int, required=True, help="Telegram user ID")

    revoke_p = sub.add_parser("user-revoke", help="Block a user session")
    revoke_p.add_argument("-t", "--telegram-id", type=int, required=True, help="Telegram user ID")

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

    # calibrate
    cal_p = sub.add_parser("calibrate", help="Calibrate agent config for local model")
    cal_p.add_argument(
        "--local-provider",
        default="default",
        help="Named provider for the local model (default: 'default')",
    )
    cal_p.add_argument(
        "--cloud-provider",
        default="cloud",
        help="Named provider for cloud analysis (default: 'cloud')",
    )
    cal_p.add_argument(
        "--scenarios",
        default="config/calibration_scenarios.yaml",
        help="Path to calibration scenarios YAML",
    )
    cal_p.add_argument(
        "--max-iterations",
        type=int,
        default=5,
        help="Max calibration iterations (default: 5)",
    )
    cal_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run scenarios and show score only, without cloud calibration",
    )
    cal_p.add_argument(
        "--reset",
        action="store_true",
        help="Clear previous calibration before starting",
    )

    return parser


# ──────────────────────────────────────────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────────────────────────────────────────


def cmd_chat(telegram_id: int, *, setup_mode: bool = False) -> None:
    """Launch an interactive CLI chat loop for a registered user."""
    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.logging.agent_logger import setup_logging
    from corpclaw_lite.paths import PROJECT_ROOT

    _settings = load_settings(PROJECT_ROOT / "config" / "settings.yaml")
    _log = _settings.logging
    setup_logging(
        log_dir=PROJECT_ROOT / _log.log_dir,
        level=_log.level,
        console_level=_log.console_level,
    )

    shutdown: asyncio.Event = asyncio.Event()

    # Daemon-thread executor for blocking input() — daemon threads
    # do NOT prevent process exit, unlike the default ThreadPoolExecutor.
    _input_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="cli-input",
    )

    async def _run() -> None:
        from pathlib import Path

        from corpclaw_lite.agent.factory import build_agent_stack
        from corpclaw_lite.channels.cli import CLIChannel
        from corpclaw_lite.config.bootstrap import BootstrapLoader
        from corpclaw_lite.runtime.shutdown import install_signal_handlers
        from corpclaw_lite.users.manager import UserManager

        install_signal_handlers(shutdown)

        stack = build_agent_stack()
        agent_loop = stack.loop
        tool_registry = stack.tool_registry
        mcp_manager = stack.mcp_manager
        _container_manager = stack.container_manager
        bootstrap = BootstrapLoader(Path("config/bootstrap"))
        system_prompt = bootstrap.get_system_prompt() or None

        from corpclaw_lite.extensions.bootstrap import load_extensions

        skill_registry, plugin_registry, skill_matcher = load_extensions(
            PROJECT_ROOT,
            tool_registry,
            _settings.skills,
        )

        # Load user from DB — same flow as Telegram bot
        standalone_manager = UserManager()
        user = standalone_manager.get_by_telegram_id(telegram_id)
        if user is None:
            print(
                f"\n[ERROR] Пользователь с telegram_id={telegram_id} не найден в БД.\n"
                f"Сначала создайте его:\n"
                f"  uv run corpclaw-lite user-create -t {telegram_id} -d <department> -n <name>\n"
            )
            return
        print(f"[INFO] Вошёл как: {user.name} (department={user.department})")
        if _container_manager and user.telegram_id is not None:
            await _container_manager.ensure_running_async(user.telegram_id)

        # ── Onboarding ─────────────────────────────────────────────────────────────
        from corpclaw_lite.onboarding.engine import OnboardingEngine
        from corpclaw_lite.onboarding.finalizer import OnboardingFinalizer
        from corpclaw_lite.onboarding.storage import OnboardingStorage

        onboarding_storage = OnboardingStorage(db_path=Path("data/users.db"))
        _cli_memory = agent_loop.memory
        onboarding_engine: OnboardingEngine | None = None
        if _cli_memory is not None:
            onboarding_finalizer = OnboardingFinalizer(
                provider=agent_loop.provider,
                memory=_cli_memory,
                bootstrap_users_dir=Path("config/bootstrap/users"),
                user_manager=standalone_manager,
            )
            onboarding_engine = OnboardingEngine(onboarding_storage, onboarding_finalizer)

        run_onboarding = onboarding_engine is not None and (
            setup_mode or await onboarding_engine.needs_onboarding(telegram_id)
        )
        if run_onboarding:
            assert onboarding_engine is not None  # guaranteed by run_onboarding guard
            if setup_mode:
                await onboarding_engine.reset(telegram_id)
            print("\n🎯 Давай настроим общение!")
            print("   (Напиши 'skip' чтобы пропустить вопрос)\n")
            question = await onboarding_engine.start(telegram_id, user.department)
            while question is not None:
                prompt_text = question.prompt
                if question.hint:
                    prompt_text += f"\n   ({question.hint})"
                print(prompt_text)
                try:
                    answer = await asyncio.get_running_loop().run_in_executor(
                        _input_executor, lambda: input("> ")
                    )
                except (EOFError, KeyboardInterrupt):
                    print("\n❌ Настройка прервана.")
                    return
                if answer.strip().lower() == "skip" and question.skippable:
                    answer = ""
                question = await onboarding_engine.submit_answer(
                    telegram_id, answer, user.department
                )
            # Refresh user from DB (name may have changed)
            user = standalone_manager.get_by_telegram_id(telegram_id) or user
            print(f"\n✅ Настройка завершена, {user.name}!\n")

        # Per-user prompt (static, set once)
        from corpclaw_lite.agent.prompt import build_skill_block

        user_prompt = bootstrap.get_user_prompt(telegram_id)
        if user_prompt:
            system_prompt = (system_prompt or "") + "\n\n" + user_prompt

        # Base system prompt without skills — skills are injected per-message
        base_system_prompt = system_prompt

        # Connect MCP servers (no hot-reload — CLI session is short-lived)
        if mcp_manager is not None:
            try:
                mcp_count = await mcp_manager.connect_all(tool_registry)
                logger.info("MCP: %d tools registered", mcp_count)
            except Exception as e:
                logger.warning("MCP init failed, continuing without MCP tools: %s", e)

        channel = CLIChannel()
        await channel.start()
        print("CorpClaw Lite – CLI chat (Ctrl+C to quit)")
        loop = asyncio.get_running_loop()
        _activity_logger = None
        try:
            while not shutdown.is_set():
                # Race input() against the shutdown event so Ctrl+C exits immediately.
                # IMPORTANT: use daemon-thread executor — the default executor
                # blocks process exit because it waits for all threads to finish,
                # but input() in a thread CANNOT be cancelled.
                input_future: asyncio.Future[str] = loop.run_in_executor(
                    _input_executor, lambda: input("You: ")
                )
                shutdown_wait = asyncio.ensure_future(shutdown.wait())
                _, _ = await asyncio.wait(
                    [input_future, shutdown_wait],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if shutdown.is_set():
                    input_future.cancel()
                    break
                # input finished first — cancel the shutdown watcher
                shutdown_wait.cancel()
                try:
                    msg = input_future.result()
                except Exception:
                    break  # EOFError / piped input exhausted

                if msg.strip():
                    # Per-message skill matching (same as Telegram runner)
                    allowed_skills = skill_registry.get_allowed_skills(user)
                    plugin_skills = [
                        p.skill
                        for p in plugin_registry.get_allowed_plugins(user)
                        if p.skill is not None
                    ]
                    all_candidate_skills = allowed_skills + plugin_skills
                    if skill_matcher is not None:
                        matched_skills = skill_matcher.match(msg, all_candidate_skills)
                    else:
                        matched_skills = all_candidate_skills
                    skill_block = build_skill_block(matched_skills, [])
                    system_prompt = base_system_prompt
                    if skill_block:
                        system_prompt = (system_prompt or "") + skill_block

                    async def approval_cb(action: str, details: str) -> bool:
                        return await channel.request_approval(user, action, details)

                    reply, run_stats = await agent_loop.run(
                        user,
                        msg,
                        system_prompt=system_prompt,
                        approval_callback=approval_cb,
                    )

                    # ── Structured activity log ────────────────────────────────────────
                    if _activity_logger is None:
                        from corpclaw_lite.logging.agent_logger import AgentLogger

                        _log = _settings.logging
                        _activity_logger = AgentLogger(log_dir=PROJECT_ROOT / _log.log_dir)
                    _activity_logger.log_request(
                        user_id=str(telegram_id),
                        department=user.department,
                        message_preview=msg[:100],
                        duration_ms=run_stats.duration_ms,
                        tools_used=run_stats.tools_used,
                        status=run_stats.status,
                        error=run_stats.error,
                    )

                    await channel.send_message(user, reply)
        finally:
            try:
                await channel.stop()
            except Exception as e:
                logger.warning("CLI channel stop failed: %s", e)
            if mcp_manager is not None:
                try:
                    await mcp_manager.disconnect_all()
                except Exception as e:
                    logger.warning("MCP disconnect failed: %s", e)
            if _container_manager is not None and user.telegram_id is not None:
                try:
                    await _container_manager.stop_async(user.telegram_id)
                except Exception as e:
                    logger.warning("Container stop failed: %s", e)
            logger.info("CLI chat shut down cleanly for user %d.", telegram_id)

    try:
        asyncio.run(_run())
    finally:
        _input_executor.shutdown(wait=False)
        # Safety net: if a blocking input() thread is still alive,
        # force-exit so we never leave ghost processes.
        # os._exit() is necessary (not sys.exit) because sys.exit raises
        # SystemExit which is caught by asyncio, leaving the daemon thread
        # alive indefinitely. os._exit terminates the process immediately.
        for t in threading.enumerate():
            if t.name.startswith("cli-input") and t.is_alive():
                logger.debug("Force-exiting: blocking input thread still alive.")
                os._exit(0)


def cmd_telegram() -> None:
    """Start the Telegram bot in polling mode."""
    token = _require_env("TELEGRAM_BOT_TOKEN")
    _require_env("CORPCLAW_IPC_SECRET")  # fail fast

    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.logging.agent_logger import setup_logging
    from corpclaw_lite.paths import PROJECT_ROOT

    _settings = load_settings(PROJECT_ROOT / "config" / "settings.yaml")
    _log = _settings.logging
    setup_logging(
        log_dir=PROJECT_ROOT / _log.log_dir,
        level=_log.level,
        console_level=_log.console_level,
    )

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


def cmd_user_allow(telegram_id: int, department: str) -> None:
    """Add a telegram_id to the bot whitelist."""
    from corpclaw_lite.users.manager import UserManager

    manager = UserManager()
    manager.add_to_whitelist(telegram_id, department)
    print(f"✅ telegram_id={telegram_id} added to whitelist (department={department})")


def cmd_user_deny(telegram_id: int) -> None:
    """Remove a telegram_id from the bot whitelist."""
    from corpclaw_lite.users.manager import UserManager

    manager = UserManager()
    removed = manager.remove_from_whitelist(telegram_id)
    if removed:
        print(f"✅ telegram_id={telegram_id} removed from whitelist")
    else:
        print(f"⚠️  telegram_id={telegram_id} was not in whitelist")


def cmd_user_revoke(telegram_id: int) -> None:
    """Block a user's session so the bot ignores their messages."""
    from corpclaw_lite.users.manager import UserManager

    manager = UserManager()
    manager.revoke_session(telegram_id)
    print(f"✅ Session revoked for telegram_id={telegram_id}")


def cmd_containers() -> None:
    """List active Docker sandbox containers."""
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


def cmd_calibrate(
    local_provider: str,
    cloud_provider: str,
    scenarios: str,
    max_iterations: int,
    dry_run: bool,
    reset: bool,
) -> None:
    """Run the calibration phase to adapt config for the local model."""
    from pathlib import Path

    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.logging.agent_logger import setup_logging
    from corpclaw_lite.paths import PROJECT_ROOT

    _settings = load_settings(PROJECT_ROOT / "config" / "settings.yaml")
    _log = _settings.logging
    setup_logging(
        log_dir=PROJECT_ROOT / _log.log_dir,
        level=_log.level,
        console_level=_log.console_level,
    )

    if reset:
        from corpclaw_lite.calibration.editor import ConfigEditor

        editor = ConfigEditor(PROJECT_ROOT)
        editor.reset()
        print("Cleared previous calibration.")

    from corpclaw_lite.calibration.loop import CalibrationLoop

    loop = CalibrationLoop(
        local_provider_name=local_provider,
        cloud_provider_name=cloud_provider,
        scenarios_path=Path(scenarios),
        project_root=PROJECT_ROOT,
        max_iterations=max_iterations,
        dry_run=dry_run,
    )
    asyncio.run(loop.run())


def cmd_generate(ext_type: str, name: str) -> None:
    """Scaffold a new skill, plugin, or subagent."""
    from pathlib import Path

    from corpclaw_lite.templates import (
        PLUGIN_MANIFEST_TEMPLATE,
        PLUGIN_SKILL_TEMPLATE,
        SKILL_TEMPLATE,
        SUBAGENT_TEMPLATE,
    )

    title = name.replace("_", " ").replace("-", " ").title()

    if ext_type == "skill":
        path = Path("skills") / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SKILL_TEMPLATE.format(name=name, title=title), encoding="utf-8")
        print(f"Created skill: {path}")

    elif ext_type == "plugin":
        base = Path("plugins") / name
        base.mkdir(parents=True, exist_ok=True)
        (base / "manifest.yaml").write_text(
            PLUGIN_MANIFEST_TEMPLATE.format(name=name), encoding="utf-8"
        )
        (base / "skill.md").write_text(
            PLUGIN_SKILL_TEMPLATE.format(name=name, title=title), encoding="utf-8"
        )
        print(f"Created plugin: {base}/")

    elif ext_type == "subagent":
        path = Path("config") / "subagents" / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SUBAGENT_TEMPLATE.format(name=name), encoding="utf-8")
        print(f"Created subagent spec: {path}")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "chat":
        cmd_chat(args.telegram_id, setup_mode=args.setup)
    elif args.command == "telegram":
        cmd_telegram()
    elif args.command == "user-list":
        cmd_user_list()
    elif args.command == "user-create":
        cmd_user_create(args.telegram_id, args.department, args.name)
    elif args.command == "user-allow":
        cmd_user_allow(args.telegram_id, args.department)
    elif args.command == "user-deny":
        cmd_user_deny(args.telegram_id)
    elif args.command == "user-revoke":
        cmd_user_revoke(args.telegram_id)
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
    elif args.command == "calibrate":
        cmd_calibrate(
            local_provider=args.local_provider,
            cloud_provider=args.cloud_provider,
            scenarios=args.scenarios,
            max_iterations=args.max_iterations,
            dry_run=args.dry_run,
            reset=args.reset,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
