"""Tests for SkillMatcher — semantic skill selection."""

from __future__ import annotations

from corpclaw_lite.extensions.skills.base import Skill
from corpclaw_lite.extensions.skills.matcher import SkillMatcher, SkillMatcherConfig


def _make_skill(
    sid: str,
    description: str = "",
    instructions: str = "",
    keywords: list[str] | None = None,
    always: bool = False,
) -> Skill:
    return Skill(
        id=sid,
        description=description,
        allowed_for=["*"],
        instructions=instructions,
        keywords=keywords or [],
        always=always,
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────

EXCEL_SKILL = _make_skill(
    "excel_normalizer",
    description="Normalize and clean Excel/CSV files — fix headers, types, duplicates",
    instructions="Use read_file to read the file if it's CSV. Identify issues.",
    keywords=["excel", "xlsx", "csv", "таблиц", "нормализ", "дубликат", "очист"],
)

TRANSLATOR_SKILL = _make_skill(
    "translator",
    description="Translate text between languages",
    instructions="Identify source language. Translate accurately.",
    keywords=["перевед", "перевод", "translat", "язык", "language", "английск", "english"],
)

DATA_ANALYST_SKILL = _make_skill(
    "data_analyst",
    description="Analyze data files, generate charts, run SQL queries, and convert formats",
    instructions="Use table_query for SQL. Generate charts with chart_generate.",
    keywords=["analyze", "анализ", "data", "данны", "chart", "sql", "запрос", "report"],
)

MEETING_SUMMARY_SKILL = _make_skill(
    "meeting_summary",
    description="Structure meeting notes into organized summaries",
    instructions="Extract summary, decisions, action items from meeting notes.",
    keywords=["meeting", "встреч", "собрани", "summary", "итог", "задач", "notes"],
)

ALL_SKILLS = [
    EXCEL_SKILL,
    TRANSLATOR_SKILL,
    DATA_ANALYST_SKILL,
    MEETING_SUMMARY_SKILL,
]


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestKeywordMatch:
    """Test that keyword matching works correctly."""

    def test_excel_keywords_match_ru(self) -> None:
        matcher = SkillMatcher()
        result = matcher.match("Нормализуй мой Excel файл", ALL_SKILLS)
        ids = [s.id for s in result]
        assert "excel_normalizer" in ids

    def test_excel_keywords_match_en(self) -> None:
        matcher = SkillMatcher()
        result = matcher.match("normalize this CSV file", ALL_SKILLS)
        ids = [s.id for s in result]
        assert "excel_normalizer" in ids

    def test_translator_match_ru(self) -> None:
        matcher = SkillMatcher()
        result = matcher.match("Переведи на английский", ALL_SKILLS)
        ids = [s.id for s in result]
        assert "translator" in ids

    def test_data_analyst_match(self) -> None:
        matcher = SkillMatcher()
        result = matcher.match("Проанализируй данные из файла", ALL_SKILLS)
        ids = [s.id for s in result]
        assert "data_analyst" in ids

    def test_meeting_summary_match(self) -> None:
        matcher = SkillMatcher()
        result = matcher.match("Сделай саммари встречи", ALL_SKILLS)
        ids = [s.id for s in result]
        assert "meeting_summary" in ids


class TestExclusion:
    """Test that irrelevant skills are NOT included."""

    def test_excel_query_excludes_translator(self) -> None:
        matcher = SkillMatcher()
        result = matcher.match("Нормализуй мой Excel файл", ALL_SKILLS)
        ids = [s.id for s in result]
        assert "translator" not in ids

    def test_translate_query_excludes_excel(self) -> None:
        matcher = SkillMatcher()
        result = matcher.match("Переведи текст на английский", ALL_SKILLS)
        ids = [s.id for s in result]
        assert "excel_normalizer" not in ids

    def test_greeting_returns_nothing(self) -> None:
        """A simple greeting should not match any skills."""
        matcher = SkillMatcher()
        result = matcher.match("Привет, как дела?", ALL_SKILLS)
        assert len(result) == 0


class TestAlwaysSkills:
    """Test always=True behaviour."""

    def test_always_skill_always_included(self) -> None:
        help_skill = _make_skill("help", description="General help", always=True)
        skills = ALL_SKILLS + [help_skill]
        matcher = SkillMatcher()
        result = matcher.match("Нормализуй Excel", skills)
        ids = [s.id for s in result]
        assert "help" in ids
        assert "excel_normalizer" in ids

    def test_always_skill_on_greeting(self) -> None:
        help_skill = _make_skill("help", description="General help", always=True)
        matcher = SkillMatcher()
        result = matcher.match("Привет", [help_skill] + ALL_SKILLS)
        ids = [s.id for s in result]
        assert "help" in ids


class TestTopK:
    """Test that top_k limit is respected."""

    def test_top_k_limits_results(self) -> None:
        config = SkillMatcherConfig(top_k=1)
        matcher = SkillMatcher(config)
        # This message could match multiple skills, but top_k=1
        result = matcher.match("analyze data and normalize excel table", ALL_SKILLS)
        # always skills excluded from top_k count, so at most 1 non-always skill
        non_always = [s for s in result if not s.always]
        assert len(non_always) <= 1


class TestDisabled:
    """Test that disabling returns all skills."""

    def test_disabled_returns_all(self) -> None:
        config = SkillMatcherConfig(enabled=False)
        matcher = SkillMatcher(config)
        result = matcher.match("anything", ALL_SKILLS)
        assert len(result) == len(ALL_SKILLS)


class TestEmptyInput:
    """Edge cases."""

    def test_empty_skills_list(self) -> None:
        matcher = SkillMatcher()
        result = matcher.match("Нормализуй Excel", [])
        assert result == []

    def test_empty_message(self) -> None:
        matcher = SkillMatcher()
        result = matcher.match("", ALL_SKILLS)
        assert result == []

    def test_single_word_stop_word(self) -> None:
        """A message of only stop-words should match nothing."""
        matcher = SkillMatcher()
        result = matcher.match("и в на с", ALL_SKILLS)
        assert result == []


class TestTfidfFallback:
    """Test TF-IDF matching for skills without keywords."""

    def test_skill_without_keywords_matched_by_tfidf(self) -> None:
        """A skill with no keywords but relevant description should still match via TF-IDF."""
        no_kw_skill = _make_skill(
            "excel_helper",
            description="Process Excel spreadsheets and normalize data columns",
            instructions="Read the Excel file. Fix column names. Remove duplicates.",
        )
        matcher = SkillMatcher()
        result = matcher.match("normalize Excel spreadsheet", [no_kw_skill])
        ids = [s.id for s in result]
        assert "excel_helper" in ids


class TestIndexRebuild:
    """Test that the index rebuilds when skills change."""

    def test_index_rebuilds_on_skill_change(self) -> None:
        matcher = SkillMatcher()
        # First call builds index
        r1 = matcher.match("Нормализуй Excel", ALL_SKILLS)
        assert any(s.id == "excel_normalizer" for s in r1)

        # Second call with different skills should rebuild
        r2 = matcher.match("Переведи текст", [TRANSLATOR_SKILL])
        ids = [s.id for s in r2]
        assert "translator" in ids
        assert "excel_normalizer" not in ids


class TestScopeFilter:
    """Test that scope field is part of Skill and can be used for filtering."""

    def test_scope_default_is_star(self) -> None:
        skill = _make_skill("test", description="test")
        assert skill.scope == ["*"]

    def test_scope_custom_value(self) -> None:
        skill = Skill(
            id="test",
            description="test",
            allowed_for=["*"],
            instructions="",
            scope=["data-agent"],
        )
        assert skill.scope == ["data-agent"]

    def test_scope_filtering(self) -> None:
        main_skill = Skill(
            id="main_only",
            description="for main",
            allowed_for=["*"],
            instructions="",
            scope=["main"],
        )
        sub_skill = Skill(
            id="sub_only",
            description="for sub",
            allowed_for=["*"],
            instructions="",
            scope=["data-agent"],
        )
        all_skills = [main_skill, sub_skill]

        # Filter for main agent
        main_scoped = [s for s in all_skills if "*" in s.scope or "main" in s.scope]
        assert len(main_scoped) == 1
        assert main_scoped[0].id == "main_only"

        # Filter for data-agent
        sub_scoped = [s for s in all_skills if "*" in s.scope or "data-agent" in s.scope]
        assert len(sub_scoped) == 1
        assert sub_scoped[0].id == "sub_only"
