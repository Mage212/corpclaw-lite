"""Scaffolding templates for ``corpclaw-lite generate`` CLI command."""

SKILL_TEMPLATE = """\
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

PLUGIN_MANIFEST_TEMPLATE = """\
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

PLUGIN_SKILL_TEMPLATE = """\
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

SUBAGENT_TEMPLATE = """\
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
