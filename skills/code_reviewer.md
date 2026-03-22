---
id: code_reviewer
description: Review code for bugs, style issues, and best practices
version: "1.0.0"
allowed_for:
  - engineering
  - it
  - default
---

# Code Reviewer Skill

## Context

Use this skill when a developer shares code for review. Focus on:
- Correctness and logic bugs
- Security vulnerabilities (injection, auth, secrets in code)
- Performance issues (N+1 queries, unnecessary allocations)
- Code style and readability
- Missing error handling or edge cases

## Instructions

1. Read the provided code carefully.
2. Structure your review with sections: **Bugs**, **Security**, **Performance**, **Style**, **Suggestions**.
3. For each issue, include: the line/location, what the problem is, and a concrete fix.
4. Highlight critical issues (security, data loss) prominently.
5. Acknowledge what is done well — not just problems.
6. If the code is good, say so briefly.

## Examples

**Input:** "Review this Python function: def get_user(id): return db.execute(f'SELECT * FROM users WHERE id={id}')"
**Output:**
- **Security (CRITICAL):** SQL injection on line 1. Use parameterized queries:
  `db.execute('SELECT * FROM users WHERE id = ?', (id,))`
- **Style:** Function missing type annotations and docstring.
