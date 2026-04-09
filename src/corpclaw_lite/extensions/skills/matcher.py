"""Semantic skill selection — keyword + TF-IDF matching.

Selects only the most relevant skills for a user message instead of
injecting all of them into the system prompt.  Zero external dependencies.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field

from corpclaw_lite.extensions.skills.base import Skill

__all__ = [
    "SkillMatcher",
    "SkillMatcherConfig",
]

logger = logging.getLogger(__name__)

# ── Tokenisation helpers ──────────────────────────────────────────────────────

_SPLIT_RE = re.compile(r"[^a-zа-яёA-ZА-ЯЁ0-9]+")

# Bilingual stop-words (RU + EN) — common function words that add noise.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        # Russian
        "и",
        "в",
        "на",
        "с",
        "по",
        "из",
        "у",
        "к",
        "о",
        "за",
        "от",
        "до",
        "для",
        "не",
        "но",
        "да",
        "то",
        "это",
        "как",
        "что",
        "все",
        "так",
        "его",
        "её",
        "их",
        "они",
        "мы",
        "вы",
        "он",
        "она",
        "оно",
        "мне",
        "мой",
        "моя",
        "мои",
        "ваш",
        "уже",
        "ещё",
        "тоже",
        "только",
        "можно",
        "нужно",
        "этот",
        "эта",
        "эти",
        "тот",
        "та",
        "те",
        "быть",
        "был",
        "была",
        "были",
        "есть",
        "будет",
        "будут",
        "бы",
        "же",
        "ли",
        "чтобы",
        "если",
        "при",
        "или",
        "ни",
        "без",
        "когда",
        "где",
        "кто",
        "чем",
        "через",
        "потом",
        "пожалуйста",
        "привет",
        "здравствуйте",
        "спасибо",
        # English
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "shall",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "about",
        "it",
        "its",
        "this",
        "that",
        "and",
        "or",
        "but",
        "if",
        "not",
        "no",
        "so",
        "up",
        "than",
        "too",
        "very",
        "just",
        "also",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "she",
        "they",
        "them",
        "his",
        "her",
        "all",
        "each",
        "which",
        "what",
        "who",
        "how",
        "when",
        "where",
        "there",
        "here",
        "please",
        "hello",
        "hi",
        "thanks",
    }
)


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanum, drop stop-words and short tokens."""
    tokens = _SPLIT_RE.split(text.lower())
    return [t for t in tokens if len(t) > 1 and t not in _STOP_WORDS]


# ── TF-IDF helpers ────────────────────────────────────────────────────────────


def _tf(tokens: list[str]) -> dict[str, float]:
    """Compute term-frequency (normalised by doc length)."""
    counts = Counter(tokens)
    length = len(tokens) or 1
    return {t: c / length for t, c in counts.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity between two sparse TF-IDF vectors."""
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b[k] for k in a if k in b)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    denom = norm_a * norm_b
    return dot / denom if denom else 0.0


# ── Cached per-skill document ─────────────────────────────────────────────────


@dataclass
class _SkillDoc:
    """Pre-computed representations for one skill."""

    skill: Skill
    tokens: list[str]
    tfidf: dict[str, float] = field(default_factory=lambda: dict[str, float]())
    kw_lower: list[str] = field(default_factory=lambda: list[str]())


# ── SkillMatcher ──────────────────────────────────────────────────────────────


@dataclass
class SkillMatcherConfig:
    """Configuration for the skill matcher."""

    enabled: bool = True
    top_k: int = 3
    tfidf_threshold: float = 0.08
    keyword_boost: float = 0.5


class SkillMatcher:
    """Selects relevant skills based on user message via keywords + TF-IDF.

    Usage::

        matcher = SkillMatcher(config)
        matched = matcher.match("Нормализуй мой Excel файл", allowed_skills)
        # → [excel_normalizer]  (only the relevant skill)
    """

    def __init__(self, config: SkillMatcherConfig | None = None) -> None:
        self._cfg = config or SkillMatcherConfig()
        # Cached index — rebuilt lazily when skill list identity changes.
        self._docs: list[_SkillDoc] = []
        self._idf: dict[str, float] = {}
        self._indexed_ids: frozenset[str] = frozenset()
        self._content_digest: int = 0

    # ── Public API ────────────────────────────────────────────────────────

    def match(self, message: str, allowed_skills: list[Skill]) -> list[Skill]:
        """Return the most relevant skills for *message*.

        Matching priority:
        1. Skills with ``always=True`` — unconditionally included.
        2. Skills with a keyword hit — included (capped by top_k).
        3. TF-IDF similarity above threshold — included (capped by top_k).
        4. Skills with **no keywords defined** — included as fallback
           (backward-compat: old skills without keywords stay visible).

        Returns at most ``top_k`` skills (plus any ``always`` skills on top).
        """
        if not self._cfg.enabled:
            return list(allowed_skills)

        if not allowed_skills:
            return []

        self._ensure_index(allowed_skills)
        msg_tokens = _tokenize(message)

        if not msg_tokens:
            # Empty/trivial message — return only always-skills
            return [s for s in allowed_skills if s.always]

        # Compute TF-IDF vector for the query
        msg_tf = _tf(msg_tokens)
        msg_tfidf = {t: tf_val * self._idf.get(t, 0.0) for t, tf_val in msg_tf.items()}

        scored: list[tuple[float, Skill]] = []
        always_out: list[Skill] = []

        for doc in self._docs:
            if doc.skill.always:
                always_out.append(doc.skill)
                continue

            # Keyword matching — prefix-based so "нормализ" matches "нормализуй"
            kw_score = self._keyword_score(msg_tokens, doc.kw_lower)

            # TF-IDF cosine similarity
            tfidf_score = _cosine(msg_tfidf, doc.tfidf)

            # Combined score
            combined = tfidf_score + kw_score * self._cfg.keyword_boost

            # Skills without keywords get a small baseline so they still appear
            # when nothing else matches (backward compat).
            if not doc.kw_lower and tfidf_score > 0:
                combined = max(combined, tfidf_score)

            if combined >= self._cfg.tfidf_threshold:
                scored.append((combined, doc.skill))

        # Sort by score descending, take top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [s for _, s in scored[: self._cfg.top_k]]

        result = always_out + top
        if result:
            logger.debug(
                "SkillMatcher: selected %d skill(s) for message %r: %s",
                len(result),
                message[:80],
                [s.id for s in result],
            )
        return result

    # ── Internal ─────────────────────────────────────────────────────────

    @staticmethod
    def _keyword_score(msg_tokens: list[str], kw_lower: list[str]) -> float:
        """Check if any message token starts with any skill keyword (prefix match).

        Returns 1.0 if **any** keyword matches, 0.0 otherwise (binary).
        This ensures that a single keyword hit always guarantees inclusion
        regardless of how many total keywords a skill defines.
        """
        if not kw_lower:
            return 0.0
        for kw in kw_lower:
            for tok in msg_tokens:
                if tok.startswith(kw) or kw.startswith(tok):
                    return 1.0
        return 0.0

    def _ensure_index(self, skills: list[Skill]) -> None:
        """Rebuild the TF-IDF index if the skill set changed."""
        current_ids = frozenset(s.id for s in skills)
        if current_ids == self._indexed_ids and self._docs:
            current_digest = hash(
                tuple((s.id, s.description, s.instructions[:300]) for s in skills)
            )
            if current_digest == self._content_digest:
                return
        self._rebuild_index(skills)

    def _rebuild_index(self, skills: list[Skill]) -> None:
        """Build TF-IDF index from scratch."""
        docs: list[_SkillDoc] = []
        for skill in skills:
            # Build document text: id + description + first N chars of instructions
            doc_text = f"{skill.id} {skill.description} {skill.instructions[:300]}"
            tokens = _tokenize(doc_text)
            kw_lower = [k.lower() for k in skill.keywords]
            docs.append(_SkillDoc(skill=skill, tokens=tokens, kw_lower=kw_lower))

        # Compute IDF across all skill documents
        n_docs = len(docs)
        df: Counter[str] = Counter()
        for doc in docs:
            unique = set(doc.tokens)
            for t in unique:
                df[t] += 1

        idf: dict[str, float] = {}
        for term, count in df.items():
            idf[term] = math.log((n_docs + 1) / (count + 1)) + 1  # smoothed IDF

        # Compute TF-IDF for each document
        for doc in docs:
            tf_vals = _tf(doc.tokens)
            doc.tfidf = {t: tf_val * idf.get(t, 0.0) for t, tf_val in tf_vals.items()}

        self._docs = docs
        self._idf = idf
        self._indexed_ids = frozenset(s.id for s in skills)
        self._content_digest = hash(
            tuple((s.id, s.description, s.instructions[:300]) for s in skills)
        )
        logger.debug("SkillMatcher: rebuilt index for %d skills", len(docs))
