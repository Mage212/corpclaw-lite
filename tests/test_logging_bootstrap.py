import pytest
from pathlib import Path
from corpclaw_lite.config.bootstrap import BootstrapLoader
from corpclaw_lite.logging.agent_logger import AgentLogger
from corpclaw_lite.extensions.skills.watcher import SkillHotReloader
from corpclaw_lite.extensions.skills.registry import SkillRegistry


# ──────────────────────────────────────────────────────────────────────────────
# BootstrapLoader
# ──────────────────────────────────────────────────────────────────────────────

def test_bootstrap_empty_dir(tmp_path):
    loader = BootstrapLoader(tmp_path)
    assert loader.get_system_prompt() == ""


def test_bootstrap_single_file(tmp_path):
    (tmp_path / "SOUL.md").write_text("# Soul\nYou are an agent.", encoding="utf-8")
    loader = BootstrapLoader(tmp_path)
    prompt = loader.get_system_prompt()
    assert "You are an agent." in prompt


def test_bootstrap_multiple_files_joined(tmp_path):
    (tmp_path / "SOUL.md").write_text("Soul content", encoding="utf-8")
    (tmp_path / "COMPANY.md").write_text("Company content", encoding="utf-8")
    loader = BootstrapLoader(tmp_path)
    prompt = loader.get_system_prompt()
    assert "Soul content" in prompt
    assert "Company content" in prompt
    assert "---" in prompt  # separator between sections


def test_bootstrap_extras_injected(tmp_path):
    loader = BootstrapLoader(tmp_path)
    prompt = loader.get_system_prompt(extras={"Skills": "- skill_one: does stuff"})
    assert "## Skills" in prompt
    assert "skill_one" in prompt


def test_bootstrap_hot_reload(tmp_path):
    p = tmp_path / "SOUL.md"
    p.write_text("version one", encoding="utf-8")
    loader = BootstrapLoader(tmp_path)
    assert "version one" in loader.get_system_prompt()

    import time
    time.sleep(0.02)  # ensure mtime changes
    p.write_text("version two", encoding="utf-8")
    # Force a new mtime by touching the file
    p.touch()

    assert "version two" in loader.get_system_prompt()


def test_render_skills_section():
    loader = BootstrapLoader("/non/existent")
    section = loader.render_skills_section([("data_analysis", "Analyse tables")])
    assert "data_analysis" in section
    assert "Analyse tables" in section


# ──────────────────────────────────────────────────────────────────────────────
# AgentLogger
# ──────────────────────────────────────────────────────────────────────────────

def test_agent_logger_writes_json(tmp_path):
    logger = AgentLogger(log_dir=tmp_path)
    logger.log_request(
        user_id="u1",
        department="HR",
        message_preview="Normalize this Excel",
        duration_ms=1234.5,
        tools_used=["write_file", "read_file"],
        status="ok",
    )
    log_file = tmp_path / "agent_activity.jsonl"
    assert log_file.exists()
    import json
    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert record["user_id"] == "u1"
    assert record["tool_count"] == 2
    assert record["status"] == "ok"
    assert "Normalize" in record["message_preview"]


def test_agent_logger_error_field(tmp_path):
    logger = AgentLogger(log_dir=tmp_path)
    logger.log_request(
        user_id="u2",
        department="IT",
        message_preview="Do something",
        duration_ms=0.0,
        tools_used=[],
        status="error",
        error="Budget exceeded",
    )
    import json
    record = json.loads((tmp_path / "agent_activity.jsonl").read_text().strip())
    assert record["error"] == "Budget exceeded"


# ──────────────────────────────────────────────────────────────────────────────
# SkillHotReloader
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skill_hot_reloader_loads(tmp_path):
    # Write a valid skill markdown
    skill_content = """\
---
id: test_skill
description: A test skill
version: "1.0.0"
allowed_for:
  - "*"
---

# Test Skill

## Instructions
Do the thing.
"""
    (tmp_path / "test_skill.md").write_text(skill_content, encoding="utf-8")

    registry = SkillRegistry()
    reloader = SkillHotReloader(tmp_path, registry, poll_interval=0.05)
    reloader.start()

    import asyncio
    await asyncio.sleep(0.15)  # let it poll once
    reloader.stop()

    assert registry.get_skill("test_skill") is not None
