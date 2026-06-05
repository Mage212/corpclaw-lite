from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
TRANSLIT_MARKERS = (
    "Vypolnyaet",
    "Otlicno",
    "Ekspert",
    "rabote",
    "failovoi",
    "poiskom",
    "compiliruet",
    "podhodit",
    "zapuska",
    "skripty",
    "testy",
    "navigacii",
)


def _lines_with_cyrillic(path: Path) -> list[tuple[int, str]]:
    return [
        (index, line)
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if CYRILLIC_RE.search(line)
    ]


def test_subagent_system_prompts_are_english_only() -> None:
    prompt_dir = PROJECT_ROOT / "config" / "bootstrap" / "subagents"

    offenders: list[str] = []
    for path in sorted(prompt_dir.glob("*.md")):
        for line_no, line in _lines_with_cyrillic(path):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line_no}: {line}")

    assert offenders == []


def test_core_bootstrap_cyrillic_is_only_explicit_examples() -> None:
    checked = [
        PROJECT_ROOT / "config" / "bootstrap" / "SOUL.md",
        PROJECT_ROOT / "config" / "bootstrap" / "COMPANY.md",
        PROJECT_ROOT / "config" / "bootstrap" / "BEHAVIOR.md",
    ]

    offenders: list[str] = []
    for path in checked:
        for line_no, line in _lines_with_cyrillic(path):
            if "examples" in line.casefold():
                continue
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line_no}: {line}")

    assert offenders == []


def test_skill_cyrillic_is_limited_to_keywords_and_examples() -> None:
    skill_dir = PROJECT_ROOT / "skills"

    offenders: list[str] = []
    for path in sorted(skill_dir.glob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        in_frontmatter = False
        in_keywords = False
        in_examples = False
        seen_frontmatter_start = False

        for line_no, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped == "---":
                if not seen_frontmatter_start:
                    in_frontmatter = True
                    seen_frontmatter_start = True
                elif in_frontmatter:
                    in_frontmatter = False
                    in_keywords = False
                continue

            if stripped.startswith("## Examples") or stripped.startswith("## Example"):
                in_examples = True

            if in_frontmatter:
                if stripped.startswith("keywords:"):
                    in_keywords = True
                elif re.match(r"^[A-Za-z_]+:", stripped):
                    in_keywords = False

            if not CYRILLIC_RE.search(line):
                continue

            allowed = (
                in_examples
                or in_keywords
                or "pattern examples" in line.casefold()
                or "column name examples" in line.casefold()
                or "user date phrase example" in line.casefold()
            )
            if not allowed:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line_no}: {line}")

    assert offenders == []


def test_builtin_subagent_metadata_has_no_cyrillic_or_translit() -> None:
    metadata_dirs = [
        PROJECT_ROOT / "config" / "subagents",
        PROJECT_ROOT / "src" / "corpclaw_lite" / "extensions" / "subagents" / "builtin",
    ]

    offenders: list[str] = []
    for metadata_dir in metadata_dirs:
        for path in sorted(metadata_dir.glob("*.yaml")):
            text = path.read_text(encoding="utf-8")
            for line_no, line in _lines_with_cyrillic(path):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line_no}: {line}")
            offenders.extend(
                f"{path.relative_to(PROJECT_ROOT)}: contains {marker!r}"
                for marker in TRANSLIT_MARKERS
                if marker in text
            )

    assert offenders == []


def test_onboarding_prompt_cyrillic_is_only_examples_and_skip_tokens() -> None:
    path = PROJECT_ROOT / "src" / "corpclaw_lite" / "onboarding" / "finalizer.py"
    lines = path.read_text(encoding="utf-8").splitlines()

    offenders: list[str] = []
    in_russian_example = False
    for line_no, line in enumerate(lines, start=1):
        if "Example response for Russian answers:" in line:
            in_russian_example = True
        elif in_russian_example and "===END===" in line:
            in_russian_example = False

        if not CYRILLIC_RE.search(line):
            continue

        allowed = (
            in_russian_example
            or "imperative phrases" in line
            or '"Общайся' in line
            or '"Используй' in line
            or '"Отвечай' in line
            or "locale-specific negative answer" in line
            or '"нет"' in line
        )
        if not allowed:
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line_no}: {line}")

    assert offenders == []
