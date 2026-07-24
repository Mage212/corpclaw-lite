# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
"""B-108 / D-080: Memora-style memory entries + hybrid recall (SQLite + FTS5)."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from functools import partial
from typing import Any, cast

import anyio

from corpclaw_lite.exceptions import StorageError
from corpclaw_lite.paths import DATA_DIR
from corpclaw_lite.utils.db import db_connect

__all__ = [
    "SQLiteMemory",
]

logger = logging.getLogger(__name__)

_DATA_DIR = DATA_DIR
_WS_RE = re.compile(r"\s+")
_MAX_ABSTRACTION_LEN = 120
_MAX_CUES = 16
_MAX_CUE_LEN = 64
# Cap stored fact value length so an unbounded model/agent value cannot bloat the
# memory table (and thus the context budget when recalled).
_MAX_VALUE_LEN = 8000
# Cap on the fallback full-scan used to boost short-token cue matches.
_FALLBACK_RECALL_LIMIT = 500
_FTS_WEIGHT = 2.0
_CUE_EXACT_WEIGHT = 1.0


def _normalize_abstraction(text: str) -> str:
    cleaned = _WS_RE.sub(" ", (text or "").strip())
    if len(cleaned) > _MAX_ABSTRACTION_LEN:
        cleaned = cleaned[:_MAX_ABSTRACTION_LEN].rstrip()
    return cleaned


def _normalize_cues(cues: list[str] | None) -> list[str]:
    if not cues:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in cues:
        c = _WS_RE.sub(" ", raw.strip())
        if not c:
            continue
        if len(c) > _MAX_CUE_LEN:
            c = c[:_MAX_CUE_LEN].rstrip()
        key = c.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= _MAX_CUES:
            break
    return out


def _cues_to_json(cues: list[str]) -> str:
    return json.dumps(cues, ensure_ascii=False)


def _cues_from_json(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    items = cast(list[Any], data)
    return _normalize_cues([str(x) for x in items])


def _cues_fts_text(cues: list[str]) -> str:
    return " ".join(cues)


def _union_cues(existing: list[str], new: list[str]) -> list[str]:
    return _normalize_cues([*existing, *new])


class SQLiteMemory:
    """Cross-chat structured memory entries (user_id-keyed), backed by SQLite.

    B-106 / D-078: transcript lives in ``ChatContextStore`` only.
    B-108 / D-080: ``memory_entries`` (abstraction + value + cues) + FTS5 hybrid
    recall. Legacy ``store_fact`` / ``recall_facts`` remain as thin aliases.

    All public methods are async and run SQLite I/O in a worker thread.
    """

    def __init__(self, db_path: str = "memory.db"):
        self.db_path = _DATA_DIR / db_path
        self._fts_ok = True
        self._init_db()

    def _init_db(self) -> None:
        """Create entries tables; drop legacy memory_facts / messages once (S1-09).

        FTS is durable: never drop ``memory_entries_fts`` on init. If the FTS
        row count diverges from ``memory_entries`` (empty after crash, partial
        dual-process fill), rebuild from the primary table.

        S1-09: the legacy DROPs are gated behind a one-time migration marker in
        ``app_metadata`` (mirroring UserManager's pattern). A second SQLiteMemory
        instance, a CLI subcommand, or a restart during an in-flight writer can
        no longer silently drop live legacy tables — the DROP runs exactly once
        per database file.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with db_connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                # app_metadata holds one-time migration markers (S1-09).
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS app_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                # Gate the legacy clean-start DROPs behind a marker so they run
                # exactly once per DB file. Before this, every SQLiteMemory()
                # instantiation re-ran the DROPs.
                marker = conn.execute(
                    "SELECT 1 FROM app_metadata WHERE key = 'memory_legacy_drop_v1'"
                ).fetchone()
                if marker is None:
                    conn.execute("BEGIN IMMEDIATE")
                    # Double-checked locking: re-check under the write lock.
                    marker = conn.execute(
                        "SELECT 1 FROM app_metadata WHERE key = 'memory_legacy_drop_v1'"
                    ).fetchone()
                    if marker is None:
                        # Clean-start migrations (dev phase, D-078 / B-108).
                        conn.execute("DROP TABLE IF EXISTS messages")
                        conn.execute("DROP TABLE IF EXISTS memory_facts")
                        conn.execute(
                            "INSERT INTO app_metadata (key, value)"
                            " VALUES ('memory_legacy_drop_v1', 'done')"
                        )
                    conn.commit()
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_entries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        primary_abstraction TEXT NOT NULL,
                        memory_value TEXT NOT NULL,
                        cue_indices_json TEXT NOT NULL DEFAULT '[]',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, primary_abstraction)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_entries_user
                    ON memory_entries(user_id, updated_at DESC)
                    """
                )
                try:
                    conn.execute(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS memory_entries_fts USING fts5(
                            user_id UNINDEXED,
                            primary_abstraction,
                            cues,
                            tokenize = 'unicode61'
                        )
                        """
                    )
                    self._fts_ok = True
                    self._fts_heal_if_needed(conn)
                except sqlite3.OperationalError as exc:
                    logger.warning("FTS5 unavailable for memory_entries: %s", exc)
                    self._fts_ok = False
        except Exception as e:
            logger.critical("Failed to initialize SQLite Memory: %s", e)
            raise StorageError(f"Database initialization failed: {e}") from e

    # ── Vacuum ──────────────────────────────────────────────────────────────

    def _sync_vacuum(self) -> None:
        try:
            with db_connect(self.db_path) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
            logger.info("SQLite vacuum completed for %s", self.db_path)
        except Exception as e:
            logger.warning("SQLite vacuum failed: %s", e)

    async def vacuum(self) -> None:
        """Manually trigger VACUUM + WAL checkpoint to reclaim disk space."""
        await anyio.to_thread.run_sync(partial(self._sync_vacuum))

    # ── Entry storage ───────────────────────────────────────────────────────

    def _fts_upsert(
        self,
        conn: sqlite3.Connection,
        entry_id: int,
        user_id: str,
        abstraction: str,
        cues: list[str],
    ) -> None:
        if not self._fts_ok:
            return
        try:
            conn.execute("DELETE FROM memory_entries_fts WHERE rowid = ?", (entry_id,))
            conn.execute(
                """
                INSERT INTO memory_entries_fts(rowid, user_id, primary_abstraction, cues)
                VALUES (?, ?, ?, ?)
                """,
                (entry_id, str(user_id), abstraction, _cues_fts_text(cues)),
            )
        except sqlite3.OperationalError as exc:
            logger.warning("FTS upsert failed: %s", exc)
            self._fts_ok = False

    def _fts_delete_user(self, conn: sqlite3.Connection, user_id: str) -> None:
        if not self._fts_ok:
            return
        try:
            conn.execute("DELETE FROM memory_entries_fts WHERE user_id = ?", (str(user_id),))
        except sqlite3.OperationalError as exc:
            logger.warning("FTS user delete failed: %s", exc)
            self._fts_ok = False

    def _fts_rebuild(self, conn: sqlite3.Connection) -> None:
        """Rebuild FTS from ``memory_entries`` (rowid = entry id)."""
        if not self._fts_ok:
            return
        try:
            conn.execute("DELETE FROM memory_entries_fts")
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, user_id, primary_abstraction, cue_indices_json
                FROM memory_entries
                """
            ).fetchall()
            for row in rows:
                cues = _cues_from_json(str(row["cue_indices_json"] or "[]"))
                conn.execute(
                    """
                    INSERT INTO memory_entries_fts(
                        rowid, user_id, primary_abstraction, cues
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        int(row["id"]),
                        str(row["user_id"]),
                        str(row["primary_abstraction"]),
                        _cues_fts_text(cues),
                    ),
                )
            logger.info("Rebuilt memory_entries_fts: %d rows", len(rows))
        except sqlite3.OperationalError as exc:
            logger.warning("FTS rebuild failed: %s", exc)
            self._fts_ok = False

    def _fts_heal_if_needed(self, conn: sqlite3.Connection) -> None:
        """Rebuild FTS when row counts diverge from the primary table."""
        if not self._fts_ok:
            return
        try:
            entries_n = int(conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0])
            fts_n = int(conn.execute("SELECT COUNT(*) FROM memory_entries_fts").fetchone()[0])
        except (sqlite3.OperationalError, TypeError, IndexError) as exc:
            logger.warning("FTS heal count failed: %s", exc)
            self._fts_ok = False
            return
        if entries_n == 0 and fts_n == 0:
            return
        if entries_n != fts_n:
            self._fts_rebuild(conn)

    def _sync_store_entry(
        self,
        user_id: str,
        primary_abstraction: str,
        memory_value: str,
        cues: list[str] | None,
    ) -> None:
        abstraction = _normalize_abstraction(primary_abstraction)
        value = (memory_value or "").strip()
        if not abstraction:
            raise StorageError("primary_abstraction is required")
        if not value:
            raise StorageError("memory_value is required")
        # Bound the stored value so an unbounded agent/model value cannot bloat
        # the memory table and the context budget when later recalled.
        if len(value) > _MAX_VALUE_LEN:
            value = value[:_MAX_VALUE_LEN].rstrip()
        new_cues = _normalize_cues(cues)
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                existing = conn.execute(
                    """
                    SELECT id, cue_indices_json FROM memory_entries
                    WHERE user_id = ? AND primary_abstraction = ?
                    """,
                    (str(user_id), abstraction),
                ).fetchone()
                if existing is not None:
                    merged = _union_cues(_cues_from_json(existing["cue_indices_json"]), new_cues)
                    conn.execute(
                        """
                        UPDATE memory_entries
                        SET memory_value = ?,
                            cue_indices_json = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (value, _cues_to_json(merged), int(existing["id"])),
                    )
                    entry_id = int(existing["id"])
                    cues_final = merged
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO memory_entries (
                            user_id, primary_abstraction, memory_value, cue_indices_json
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (str(user_id), abstraction, value, _cues_to_json(new_cues)),
                    )
                    entry_id = int(cur.lastrowid or 0)
                    cues_final = new_cues
                if entry_id > 0:
                    self._fts_upsert(conn, entry_id, str(user_id), abstraction, cues_final)
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"Failed to store entry for user {user_id}: {e}") from e

    async def store_entry(
        self,
        user_id: str,
        *,
        primary_abstraction: str,
        memory_value: str,
        cues: list[str] | None = None,
    ) -> None:
        """Upsert a memory entry by (user_id, primary_abstraction); union cues."""
        await anyio.to_thread.run_sync(
            partial(
                self._sync_store_entry,
                user_id,
                primary_abstraction,
                memory_value,
                cues,
            )
        )

    async def store_fact(self, user_id: str, key: str, value: str) -> None:
        """Legacy alias: key → abstraction, value → memory_value."""
        await self.store_entry(user_id, primary_abstraction=key, memory_value=value, cues=None)

    # ── Recall ──────────────────────────────────────────────────────────────

    def _row_to_entry(self, row: sqlite3.Row, score: float = 0.0) -> dict[str, Any]:
        cues = _cues_from_json(row["cue_indices_json"])
        return {
            "id": int(row["id"]),
            "abstraction": str(row["primary_abstraction"]),
            "value": str(row["memory_value"]),
            "cues": cues,
            "score": score,
            # Back-compat keys for callers expecting key/value
            "key": str(row["primary_abstraction"]),
        }

    def _sync_list_recent(self, user_id: str, limit: int) -> list[dict[str, Any]]:
        with db_connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT id, primary_abstraction, memory_value, cue_indices_json
                FROM memory_entries
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (str(user_id), limit),
            )
            return [self._row_to_entry(r, score=0.0) for r in cursor.fetchall()]

    def _sync_recall_like(self, user_id: str, query: str, limit: int) -> list[dict[str, Any]]:
        like = f"%{query}%"
        with db_connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT id, primary_abstraction, memory_value, cue_indices_json
                FROM memory_entries
                WHERE user_id = ?
                  AND (
                    primary_abstraction LIKE ?
                    OR memory_value LIKE ?
                    OR cue_indices_json LIKE ?
                  )
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (str(user_id), like, like, like, max(limit * 3, 30)),
            )
            scored: list[dict[str, Any]] = []
            qfold = query.casefold()
            for r in cursor.fetchall():
                cues = _cues_from_json(r["cue_indices_json"])
                score = 0.0
                abs_l = str(r["primary_abstraction"]).casefold()
                val_l = str(r["memory_value"]).casefold()
                if qfold in abs_l:
                    score += _FTS_WEIGHT
                if qfold in val_l:
                    score += 0.5
                if any(c.casefold() == qfold for c in cues):
                    score += _CUE_EXACT_WEIGHT
                elif any(qfold in c.casefold() for c in cues):
                    score += _CUE_EXACT_WEIGHT * 0.5
                entry = self._row_to_entry(r, score=score)
                scored.append(entry)
            scored.sort(key=lambda e: (-float(e["score"]), str(e["abstraction"])))
            return scored[:limit]

    def _sync_recall_fts(self, user_id: str, query: str, limit: int) -> list[dict[str, Any]] | None:
        if not self._fts_ok:
            return None
        # FTS5 MATCH: quote tokens lightly; escape double quotes
        safe = query.replace('"', " ").strip()
        if not safe:
            return None
        # Use prefix-friendly query: each word OR'd if multi-word
        words = [w for w in safe.split() if w]
        if not words:
            return None
        match_expr = " OR ".join(f'"{w}"' if " " not in w else w for w in words)
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT e.id, e.primary_abstraction, e.memory_value, e.cue_indices_json,
                           bm25(memory_entries_fts) AS rank
                    FROM memory_entries_fts f
                    JOIN memory_entries e ON e.id = f.rowid
                    WHERE f.user_id = ? AND memory_entries_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (str(user_id), match_expr, max(limit * 3, 30)),
                )
                rows = cursor.fetchall()
        except sqlite3.OperationalError as exc:
            logger.debug("FTS recall failed, fallback LIKE: %s", exc)
            return None

        qfold = query.casefold()
        scored: list[dict[str, Any]] = []
        for r in rows:
            # bm25: lower is better in SQLite FTS5 → invert to positive score
            rank = float(r["rank"] if r["rank"] is not None else 0.0)
            fts_score = max(0.0, 10.0 - rank) * (_FTS_WEIGHT / 2.0)
            cues = _cues_from_json(r["cue_indices_json"])
            if any(c.casefold() == qfold for c in cues):
                fts_score += _CUE_EXACT_WEIGHT
            scored.append(self._row_to_entry(r, score=fts_score))

        # Also boost exact cue matches that FTS might miss (short tokens). Bounded
        # by _FALLBACK_RECALL_LIMIT so a user with many entries does not load them
        # all into memory; the FTS path above already covered the top-ranked rows.
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                all_rows = conn.execute(
                    """
                    SELECT id, primary_abstraction, memory_value, cue_indices_json
                    FROM memory_entries WHERE user_id = ?
                    LIMIT ?
                    """,
                    (str(user_id), _FALLBACK_RECALL_LIMIT),
                ).fetchall()
        except Exception:
            all_rows = []

        by_id = {int(e["id"]): e for e in scored}
        for r in all_rows:
            cues = _cues_from_json(r["cue_indices_json"])
            if not any(c.casefold() == qfold for c in cues):
                continue
            rid = int(r["id"])
            # FTS loop already applied exact-cue boost when MATCH returned the row.
            # Only add entries FTS missed (short tokens / empty index edge cases).
            if rid in by_id:
                continue
            by_id[rid] = self._row_to_entry(r, score=_CUE_EXACT_WEIGHT)

        out = list(by_id.values())
        out.sort(key=lambda e: (-float(e["score"]), str(e["abstraction"])))
        return out[:limit]

    def _sync_recall_entries(
        self, user_id: str, query: str | None, limit: int
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(100, int(limit)))
        try:
            if not query or not str(query).strip():
                return self._sync_list_recent(str(user_id), safe_limit)
            q = str(query).strip()
            fts_hits = self._sync_recall_fts(str(user_id), q, safe_limit)
            if fts_hits is not None and len(fts_hits) > 0:
                return fts_hits
            return self._sync_recall_like(str(user_id), q, safe_limit)
        except Exception as e:
            raise StorageError(f"Failed to recall entries for user {user_id}: {e}") from e

    async def recall_entries(
        self,
        user_id: str,
        query: str | None = None,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Hybrid recall: FTS on abstraction/cues + exact cue boost; LIKE fallback."""
        return await anyio.to_thread.run_sync(
            partial(self._sync_recall_entries, user_id, query, limit)
        )

    def _sync_recall_facts(
        self, user_id: str, query: str | None, limit: int
    ) -> list[dict[str, str]]:
        entries = self._sync_recall_entries(user_id, query, limit)
        return [{"key": str(e["key"]), "value": str(e["value"])} for e in entries]

    async def recall_facts(
        self, user_id: str, query: str | None = None, limit: int = 10
    ) -> list[dict[str, str]]:
        """Legacy shape: key=abstraction, value=memory_value."""
        return await anyio.to_thread.run_sync(
            partial(self._sync_recall_facts, user_id, query, limit)
        )

    def _sync_clear_facts(self, user_id: str) -> None:
        try:
            with db_connect(self.db_path) as conn:
                self._fts_delete_user(conn, str(user_id))
                conn.execute("DELETE FROM memory_entries WHERE user_id = ?", (str(user_id),))
        except Exception as e:
            raise StorageError(f"Failed to clear facts for user {user_id}: {e}") from e

    async def clear_facts(self, user_id: str) -> None:
        """Delete all memory entries for a user."""
        await anyio.to_thread.run_sync(partial(self._sync_clear_facts, user_id))
