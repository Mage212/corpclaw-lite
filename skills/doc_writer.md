---
id: doc_writer
description: Write technical documentation, READMEs, API docs, and user guides
version: "1.0.0"
allowed_for:
  - it
  - product
  - admin
  - default
keywords:
  - документац
  - document
  - readme
  - api
  - гайд
  - guide
  - инструкц
  - instruction
  - runbook
  - adr
  - описан
  - описа
  - docstring
---

# Documentation Writer Skill

## Context

Use this skill to write or improve technical documentation. This includes:
- README files for projects
- API endpoint documentation
- User guides and onboarding docs
- Architecture decision records (ADRs)
- Runbooks and operational playbooks

## Instructions

1. Identify the audience: developer, end-user, ops team.
2. Identify the format: Markdown, reStructuredText, plain text.
3. For READMEs: include Overview, Installation, Usage, Configuration, and Contributing sections.
4. For API docs: include endpoint, method, parameters, request/response examples, error codes.
5. For user guides: use numbered steps, screenshots descriptions where helpful, avoid jargon.
6. Be concise: one idea per sentence, active voice, no filler words.

## Examples

**Input:** "Write a README for a Python CLI tool called 'corpclaw-lite' that runs an AI agent."
**Output:** (full Markdown README with badges, install instructions, CLI usage examples)

**Input:** "Document this API endpoint: POST /api/users, body: {telegram_id: int, department: str}"
**Output:** (formatted API doc with request body schema, response examples, error cases)
