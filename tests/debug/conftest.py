"""Shared fixtures for CorpClaw Lite debug integration tests.

These tests run the REAL agent pipeline (real LLM, real tools, real files).
They are intentionally excluded from the default `pytest tests/` run and
must be invoked explicitly:

    uv run pytest tests/debug/ -v -m "not docker_required"

Session-scoped fixtures are used for expensive initialisation (building the
agent stack, connecting to the LLM) so that cost is paid once per session,
not once per test.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Load .env FIRST — before any project imports that read env-vars.
# config/settings.yaml uses ${VAR:-default} interpolation, so env-vars must
# be present before load_settings() is called inside build_agent_stack().
# ---------------------------------------------------------------------------
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

# Locate .env relative to this conftest: tests/debug/ -> project root
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_DOTENV_PATH = _PROJECT_ROOT / ".env"

if _DOTENV_PATH.exists():
    from dotenv import load_dotenv

    # override=False: don't clobber env-vars already exported in the shell
    load_dotenv(dotenv_path=_DOTENV_PATH, override=False)

if TYPE_CHECKING:
    from corpclaw_lite.agent.loop import AgentLoop
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.users.models import User


# ---------------------------------------------------------------------------
# Session-scoped: agent stack (no container)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def agent_stack_no_container() -> tuple[AgentLoop, ToolRegistry]:
    """Build the full agent stack in dev mode (container.enabled=false).

    Uses config/settings.yaml as-is (LLM routing, agent settings, etc.) with
    env-vars from .env already loaded above.
    Only overrides the container flag so Docker is not required.

    Session-scoped: expensive initialisation happens once per pytest session.
    """
    from corpclaw_lite.agent.factory import PROJECT_ROOT, build_agent_stack
    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.config.settings import ContainerSettings

    # Verify config exists — fail fast with a useful message
    cfg_path = PROJECT_ROOT / "config" / "settings.yaml"
    if not cfg_path.exists():
        pytest.skip(f"config/settings.yaml not found at {cfg_path} — skipping debug tests")

    # Load real settings (with .env already applied above)
    real_settings = load_settings(cfg_path)
    if not real_settings.llm.routing:
        pytest.skip("No routing rules in settings.yaml — skipping debug tests")

    # Build patched settings with container disabled — pass directly to factory
    patched_settings = real_settings.model_copy(
        update={"container": ContainerSettings(enabled=False)}
    )

    os.environ.setdefault("CORPCLAW_IPC_SECRET", "debug-test-secret")

    try:
        stack = build_agent_stack(settings=patched_settings)
        loop, registry = stack.loop, stack.tool_registry
    except RuntimeError as e:
        pytest.skip(f"Could not build agent stack: {e}")

    return loop, registry


# ---------------------------------------------------------------------------
# Session-scoped: Docker availability
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_available() -> bool:
    """Return True if Docker daemon is reachable."""
    from corpclaw_lite.container.manager import ContainerManager

    return ContainerManager.is_docker_available()


# ---------------------------------------------------------------------------
# Function-scoped: per-test fixtures
# ---------------------------------------------------------------------------


_TEST_USER_ID = 999
_TEST_TELEGRAM_ID = 999999


@pytest.fixture(scope="function")
def test_user() -> User:
    """Standard debug test user with full engineering access.

    telegram_id is set because IPCToolProxy requires it to resolve
    the correct Docker container for the user.
    """
    from corpclaw_lite.users.models import User

    return User(
        id=_TEST_USER_ID,
        name="DebugUser",
        department="engineering",
        telegram_id=_TEST_TELEGRAM_ID,
    )


@pytest.fixture(autouse=True)
def _clear_test_user_memory(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
) -> None:
    """Clear conversation memory for the test user before each test.

    Without this, history from prior test runs accumulates in SQLiteMemory
    and pollutes the LLM context — the model sees previous responses (including
    refusals, security blocks, wrong tool calls) and repeats those patterns
    instead of handling the current request fresh.
    """
    import asyncio

    loop, _ = agent_stack_no_container
    memory = loop.memory
    if memory is not None:
        asyncio.run(memory.clear(str(_TEST_USER_ID)))
        asyncio.run(memory.clear(str(_TEST_TELEGRAM_ID)))
        if hasattr(memory, "clear_facts"):
            asyncio.run(memory.clear_facts(str(_TEST_USER_ID)))
            asyncio.run(memory.clear_facts(str(_TEST_TELEGRAM_ID)))


_WORKSPACE_ROOT = _PROJECT_ROOT / "tests" / "debug" / ".workspace"


@pytest.fixture(scope="function")
def tmp_workspace(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated per-test directory under tests/debug/.workspace/ (gitignored).

    Unlike pytest's tmp_path, this directory lives inside the project tree so
    that ``resolve_and_validate_path`` (which anchors to CWD) always resolves
    paths inside the workspace even when anyio runs code in thread-pool workers
    on Windows (threads share CWD with the main process after os.chdir).

    The directory is wiped before use and left after the test for debugging.
    tests/debug/.workspace/ is listed in .gitignore.
    """
    import shutil

    # Safe name: replace characters that are invalid in directory names
    safe_name = request.node.name.replace("[", "_").replace("]", "_").replace("/", "_")
    workspace = _WORKSPACE_ROOT / safe_name
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    monkeypatch.chdir(workspace)
    return workspace
