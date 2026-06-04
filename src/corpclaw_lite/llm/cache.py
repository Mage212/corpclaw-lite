"""Persistent llama.cpp slot KV-cache management.

This module treats llama.cpp slots as temporary execution lanes and stores the
long-lived cache state in files managed by llama-server's slot save/restore API.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import httpx

from corpclaw_lite.llm.base import LLMResponse
from corpclaw_lite.llm.queue import LLMLoadClass, QueueEntry
from corpclaw_lite.logging import health
from corpclaw_lite.logging.trace import log_event
from corpclaw_lite.paths import DATA_DIR, PROJECT_ROOT
from corpclaw_lite.utils.db import db_connect

__all__ = [
    "LLMCacheLease",
    "LLMCacheManager",
    "LLMCacheMetadata",
    "LLMCacheScope",
    "PersistentCacheConfig",
    "SlotCacheActionResult",
    "SlotCacheClient",
    "config_from_settings",
]

logger = logging.getLogger(__name__)

_CACHE_DB_NAME = "index.sqlite"
_MANAGED_PREFIX = "corpclaw_"
_SUPPORTED_LOAD_CLASSES: set[LLMLoadClass] = {"interactive", "subagent"}


@dataclass(frozen=True)
class PersistentCacheConfig:
    """Runtime configuration for persistent slot cache."""

    enabled: bool = False
    root_dir: Path = DATA_DIR / "llm_cache" / "slot-cache"
    index_path: Path = DATA_DIR / "llm_cache" / _CACHE_DB_NAME
    slot_api_base_url: str | None = None
    max_total_bytes: int = 100 * 1024 * 1024 * 1024
    max_age_days: int = 30
    save_policy: Literal["hybrid", "every_response", "eviction_only"] = "hybrid"
    save_min_tokens: int = 1024
    save_dirty_seconds: float = 60.0
    validation_min_reuse_ratio: float = 0.70
    validation_large_context_tokens: int = 16_000
    validation_large_reuse_ratio: float = 0.90
    strict_mismatch_retry: bool = True
    prune_interval_seconds: float = 600.0
    http_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class LLMCacheScope:
    """Identity of one persistent cache state."""

    user_id: str
    conversation_id: str
    agent_id: str
    provider_name: str
    model: str
    preset: str | None
    system_hash: str
    tools_hash: str

    @property
    def key(self) -> str:
        payload = {
            "agent_id": self.agent_id,
            "conversation_id": self.conversation_id,
            "model": self.model,
            "preset": self.preset or "",
            "provider_name": self.provider_name,
            "system_hash": self.system_hash,
            "tools_hash": self.tools_hash,
            "user_id": self.user_id,
        }
        return _hash_payload(payload)

    @property
    def filename(self) -> str:
        return f"{_MANAGED_PREFIX}{self.key}.bin"


@dataclass(frozen=True)
class LLMCacheMetadata:
    """Persistent metadata for one cache file."""

    scope: LLMCacheScope
    filename: str
    token_count: int
    file_size_bytes: int
    prompt_tokens: int
    cached_tokens: int
    prompt_n: int
    created_at: float
    last_used_at: float
    last_saved_at: float
    save_count: int
    restore_count: int


@dataclass(frozen=True)
class SlotCacheActionResult:
    """Result returned by llama-server slot cache API."""

    ok: bool
    status_code: int
    action: str
    slot_id: int
    filename: str | None = None
    n_tokens: int = 0
    n_bytes: int = 0
    elapsed_ms: float = 0.0
    server_ms: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class LLMCacheLease:
    """Cache state associated with one acquired queue slot."""

    enabled: bool
    scope: LLMCacheScope | None = None
    slot_id: int | None = None
    hit_kind: Literal["none", "l1", "l2"] = "none"
    restored: bool = False
    metadata: LLMCacheMetadata | None = None
    expected_cached_tokens: int = 0
    restore_ms: float = 0.0


@dataclass(frozen=True)
class CacheFinalizeResult:
    """Decision after validating and optionally saving cache."""

    retry_without_cache: bool = False
    mismatch_reason: str | None = None


class SlotCacheClient(Protocol):
    """Protocol for llama.cpp slot cache API clients."""

    async def save(self, slot_id: int, *, model: str, filename: str) -> SlotCacheActionResult:
        """Save slot KV-cache into a server-side file."""
        ...

    async def restore(self, slot_id: int, *, model: str, filename: str) -> SlotCacheActionResult:
        """Restore slot KV-cache from a server-side file."""
        ...

    async def erase(self, slot_id: int, *, model: str) -> SlotCacheActionResult:
        """Erase the current slot KV-cache."""
        ...


class LlamaSlotCacheClient:
    """Small HTTP client for llama.cpp `/slots/{slot}?action=...` API."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = _normalize_slot_api_base_url(base_url)
        self._api_key = api_key
        self._timeout = httpx.Timeout(timeout_seconds)

    async def save(self, slot_id: int, *, model: str, filename: str) -> SlotCacheActionResult:
        return await self._action(slot_id, "save", model=model, filename=filename)

    async def restore(self, slot_id: int, *, model: str, filename: str) -> SlotCacheActionResult:
        return await self._action(slot_id, "restore", model=model, filename=filename)

    async def erase(self, slot_id: int, *, model: str) -> SlotCacheActionResult:
        return await self._action(slot_id, "erase", model=model, filename=None)

    async def _action(
        self,
        slot_id: int,
        action: Literal["save", "restore", "erase"],
        *,
        model: str,
        filename: str | None,
    ) -> SlotCacheActionResult:
        body: dict[str, Any] = {"model": model}
        if filename is not None:
            body["filename"] = filename
        headers: dict[str, str] = {}
        if self._api_key and self._api_key != "dummy":
            headers["Authorization"] = f"Bearer {self._api_key}"
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/slots/{slot_id}?action={action}",
                    json=body,
                    headers=headers,
                )
            elapsed_ms = (time.monotonic() - started) * 1000
            payload_raw: Any
            try:
                payload_raw = response.json()
            except ValueError:
                payload_raw = dict[str, Any]()
            payload: dict[str, Any] = (
                cast(dict[str, Any], payload_raw)
                if isinstance(payload_raw, dict)
                else dict[str, Any]()
            )
            if response.status_code >= 400:
                return SlotCacheActionResult(
                    ok=False,
                    status_code=response.status_code,
                    action=action,
                    slot_id=slot_id,
                    filename=filename,
                    elapsed_ms=elapsed_ms,
                    error=response.text[:500],
                )
            timings_raw = payload.get("timings", dict[str, Any]())
            timings: dict[str, Any] = (
                cast(dict[str, Any], timings_raw)
                if isinstance(timings_raw, dict)
                else dict[str, Any]()
            )
            n_tokens = int(
                payload.get("n_saved") or payload.get("n_restored") or payload.get("n_erased") or 0
            )
            n_bytes = int(payload.get("n_written") or payload.get("n_read") or 0)
            return SlotCacheActionResult(
                ok=True,
                status_code=response.status_code,
                action=action,
                slot_id=slot_id,
                filename=filename,
                n_tokens=n_tokens,
                n_bytes=n_bytes,
                elapsed_ms=elapsed_ms,
                server_ms=float(timings.get(f"{action}_ms", 0.0) or 0.0),
            )
        except Exception as exc:
            return SlotCacheActionResult(
                ok=False,
                status_code=0,
                action=action,
                slot_id=slot_id,
                filename=filename,
                elapsed_ms=(time.monotonic() - started) * 1000,
                error=str(exc),
            )


class LLMCacheMetadataStore:
    """SQLite-backed cache metadata index."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._initialized = False
        self._lock = asyncio.Lock()

    async def get(self, scope_key: str) -> LLMCacheMetadata | None:
        await self._ensure_initialized()
        rows = await self._execute_fetchall(
            "SELECT * FROM cache_entries WHERE scope_key = ?",
            (scope_key,),
        )
        if not rows:
            return None
        return _metadata_from_row(rows[0])

    async def upsert(self, metadata: LLMCacheMetadata) -> None:
        await self._ensure_initialized()
        scope = metadata.scope
        await self._execute_write(
            """
            INSERT INTO cache_entries (
                scope_key, user_id, conversation_id, agent_id, provider_name, model, preset,
                system_hash, tools_hash, filename, token_count, file_size_bytes,
                prompt_tokens, cached_tokens, prompt_n, created_at, last_used_at,
                last_saved_at, save_count, restore_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_key) DO UPDATE SET
                filename = excluded.filename,
                token_count = excluded.token_count,
                file_size_bytes = excluded.file_size_bytes,
                prompt_tokens = excluded.prompt_tokens,
                cached_tokens = excluded.cached_tokens,
                prompt_n = excluded.prompt_n,
                last_used_at = excluded.last_used_at,
                last_saved_at = excluded.last_saved_at,
                save_count = excluded.save_count,
                restore_count = excluded.restore_count
            """,
            (
                scope.key,
                scope.user_id,
                scope.conversation_id,
                scope.agent_id,
                scope.provider_name,
                scope.model,
                scope.preset,
                scope.system_hash,
                scope.tools_hash,
                metadata.filename,
                metadata.token_count,
                metadata.file_size_bytes,
                metadata.prompt_tokens,
                metadata.cached_tokens,
                metadata.prompt_n,
                metadata.created_at,
                metadata.last_used_at,
                metadata.last_saved_at,
                metadata.save_count,
                metadata.restore_count,
            ),
        )

    async def mark_used(self, metadata: LLMCacheMetadata) -> None:
        await self._ensure_initialized()
        await self._execute_write(
            """
            UPDATE cache_entries
            SET last_used_at = ?, restore_count = ?
            WHERE scope_key = ?
            """,
            (time.time(), metadata.restore_count + 1, metadata.scope.key),
        )

    async def delete(self, scope_key: str) -> None:
        await self._ensure_initialized()
        await self._execute_write("DELETE FROM cache_entries WHERE scope_key = ?", (scope_key,))

    async def list_all(self) -> list[LLMCacheMetadata]:
        await self._ensure_initialized()
        rows = await self._execute_fetchall(
            "SELECT * FROM cache_entries ORDER BY last_used_at ASC",
            (),
        )
        return [_metadata_from_row(row) for row in rows]

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            await self._execute_write(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    scope_key TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    provider_name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    preset TEXT,
                    system_hash TEXT NOT NULL,
                    tools_hash TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    file_size_bytes INTEGER NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    cached_tokens INTEGER NOT NULL,
                    prompt_n INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL NOT NULL,
                    last_saved_at REAL NOT NULL,
                    save_count INTEGER NOT NULL,
                    restore_count INTEGER NOT NULL
                )
                """,
                (),
            )
            self._initialized = True

    async def _execute_write(self, sql: str, params: tuple[Any, ...]) -> None:
        def run() -> None:
            with db_connect(self._path) as conn:
                conn.execute(sql, params)

        await asyncio.to_thread(run)

    async def _execute_fetchall(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        def run() -> list[sqlite3.Row]:
            with db_connect(self._path) as conn:
                conn.row_factory = sqlite3.Row
                return list(conn.execute(sql, params).fetchall())

        return await asyncio.to_thread(run)


class LLMCacheManager:
    """Coordinates L1 live-slot cache and L2 persistent cache files."""

    def __init__(
        self,
        config: PersistentCacheConfig,
        *,
        provider_base_urls: dict[str, str],
        provider_api_keys: dict[str, str | None] | None = None,
        client: SlotCacheClient | None = None,
    ) -> None:
        self._config = config
        self._provider_base_urls = provider_base_urls
        self._provider_api_keys = provider_api_keys or {}
        self._client_override = client
        self._store = LLMCacheMetadataStore(config.index_path)
        self._slot_scopes: dict[int, LLMCacheScope] = {}
        self._reset_users: set[str] = set()
        self._slot_lock = asyncio.Lock()
        self._last_prune_at = 0.0
        self._cleanup_available = self._prepare_root_dir(config.root_dir)
        if config.enabled:
            logger.info(
                "Persistent LLM cache enabled: root=%s cleanup=%s max_total_bytes=%d",
                config.root_dir,
                self._cleanup_available,
                config.max_total_bytes,
            )

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def build_scope(
        self,
        *,
        user_id: str,
        conversation_id: str,
        agent_id: str,
        provider_name: str,
        model: str,
        preset: str | None,
        system: str | None,
        tools: list[dict[str, Any]] | None,
    ) -> LLMCacheScope:
        return LLMCacheScope(
            user_id=user_id or "anonymous",
            conversation_id=conversation_id or "default",
            agent_id=agent_id or "main",
            provider_name=provider_name or "unknown",
            model=model,
            preset=preset,
            system_hash=_hash_text(system or ""),
            tools_hash=_hash_payload(tools or []),
        )

    async def mark_user_reset(self, user_id: str) -> None:
        """Mark a user's persistent cache as invalid after conversation reset.

        CorpClaw Lite currently treats one user as one active conversation stream.
        When that stream is reset, any L2 metadata for the user is removed and
        the next prepare() call erases the live slot before prompt processing.
        """
        if not self._config.enabled:
            return
        normalized_user_id = user_id or "anonymous"
        async with self._slot_lock:
            self._reset_users.add(normalized_user_id)
            stale_slots = [
                slot_id
                for slot_id, scope in self._slot_scopes.items()
                if scope.user_id == normalized_user_id
            ]
            for slot_id in stale_slots:
                self._slot_scopes.pop(slot_id, None)

        deleted = 0
        for entry in await self._store.list_all():
            if entry.scope.user_id == normalized_user_id and await self._delete_cache_entry(entry):
                deleted += 1
        log_event(
            "llm_cache_user_reset_marked",
            "unknown",
            user_id=normalized_user_id,
            deleted_entries=deleted,
        )

    async def prepare(
        self,
        entry: QueueEntry,
        scope: LLMCacheScope,
    ) -> LLMCacheLease:
        if not self._should_use_cache(entry):
            return LLMCacheLease(enabled=False)
        assert entry.slot_id is not None
        slot_id = entry.slot_id
        reset_requested = False
        async with self._slot_lock:
            if scope.user_id in self._reset_users:
                self._reset_users.remove(scope.user_id)
                self._slot_scopes.pop(slot_id, None)
                reset_requested = True
            existing_scope = self._slot_scopes.get(slot_id)
        if reset_requested:
            await self._erase_slot(entry, scope.model)
            async with self._slot_lock:
                self._slot_scopes[slot_id] = scope
            health.increment("llm_cache_reset_skip_restore")
            log_event(
                "llm_cache_reset_skip_restore",
                entry.run_id or "unknown",
                user_id=entry.user_id,
                agent_id=scope.agent_id,
                slot_id=slot_id,
                scope_key=scope.key,
            )
            return LLMCacheLease(enabled=True, scope=scope, slot_id=slot_id)
        if existing_scope is not None and existing_scope.key == scope.key:
            metadata = await self._store.get(scope.key)
            if metadata is not None:
                await self._store.mark_used(metadata)
            health.increment("llm_cache_hit_l1")
            log_event(
                "llm_cache_hit_l1",
                entry.run_id or "unknown",
                user_id=entry.user_id,
                agent_id=scope.agent_id,
                slot_id=slot_id,
                scope_key=scope.key,
            )
            return LLMCacheLease(
                enabled=True,
                scope=scope,
                slot_id=slot_id,
                hit_kind="l1",
                restored=False,
                metadata=metadata,
                expected_cached_tokens=metadata.token_count if metadata else 0,
            )

        if existing_scope is not None and existing_scope.key != scope.key:
            await self._save_slot_for_scope(
                slot_id,
                existing_scope,
                entry=entry,
                response=None,
                reason="eviction",
            )
            await self._erase_slot(entry, existing_scope.model)

        metadata = await self._store.get(scope.key)
        if metadata is None:
            async with self._slot_lock:
                self._slot_scopes[slot_id] = scope
            health.increment("llm_cache_miss")
            log_event(
                "llm_cache_miss",
                entry.run_id or "unknown",
                user_id=entry.user_id,
                agent_id=scope.agent_id,
                slot_id=slot_id,
                scope_key=scope.key,
                reason="metadata_missing",
            )
            return LLMCacheLease(enabled=True, scope=scope, slot_id=slot_id)

        log_event(
            "llm_cache_restore_started",
            entry.run_id or "unknown",
            user_id=entry.user_id,
            agent_id=scope.agent_id,
            slot_id=slot_id,
            scope_key=scope.key,
            filename=metadata.filename,
            expected_cached_tokens=metadata.token_count,
        )
        client = self._client_for(scope.provider_name)
        if client is None:
            log_event(
                "llm_cache_restore_failed",
                entry.run_id or "unknown",
                user_id=entry.user_id,
                agent_id=scope.agent_id,
                slot_id=slot_id,
                scope_key=scope.key,
                filename=metadata.filename,
                error="slot_api_base_url_missing",
            )
            async with self._slot_lock:
                self._slot_scopes[slot_id] = scope
            return LLMCacheLease(enabled=True, scope=scope, slot_id=slot_id, metadata=metadata)
        result = await client.restore(
            slot_id,
            model=scope.model,
            filename=metadata.filename,
        )
        if not result.ok:
            health.increment("llm_cache_restore_failed")
            log_event(
                "llm_cache_restore_failed",
                entry.run_id or "unknown",
                user_id=entry.user_id,
                agent_id=scope.agent_id,
                slot_id=slot_id,
                scope_key=scope.key,
                filename=metadata.filename,
                error=result.error,
                status_code=result.status_code,
            )
            async with self._slot_lock:
                self._slot_scopes[slot_id] = scope
            return LLMCacheLease(enabled=True, scope=scope, slot_id=slot_id, metadata=metadata)

        await self._store.mark_used(metadata)
        async with self._slot_lock:
            self._slot_scopes[slot_id] = scope
        health.increment("llm_cache_hit_l2")
        log_event(
            "llm_cache_restore_finished",
            entry.run_id or "unknown",
            user_id=entry.user_id,
            agent_id=scope.agent_id,
            slot_id=slot_id,
            scope_key=scope.key,
            filename=metadata.filename,
            n_restored=result.n_tokens,
            n_read=result.n_bytes,
            restore_ms=round(result.server_ms or result.elapsed_ms, 3),
        )
        return LLMCacheLease(
            enabled=True,
            scope=scope,
            slot_id=slot_id,
            hit_kind="l2",
            restored=True,
            metadata=metadata,
            expected_cached_tokens=metadata.token_count,
            restore_ms=result.server_ms or result.elapsed_ms,
        )

    async def finalize(
        self,
        entry: QueueEntry,
        lease: LLMCacheLease,
        response: LLMResponse,
        *,
        allow_retry: bool = True,
    ) -> CacheFinalizeResult:
        if not lease.enabled or lease.scope is None or lease.slot_id is None:
            return CacheFinalizeResult()

        mismatch = self._validate_reuse(entry, lease, response)
        if mismatch is not None:
            health.increment("llm_cache_mismatch")
            log_event(
                "llm_cache_mismatch",
                entry.run_id or "unknown",
                user_id=entry.user_id,
                agent_id=lease.scope.agent_id,
                slot_id=lease.slot_id,
                scope_key=lease.scope.key,
                reason=mismatch,
                prompt_tokens=response.usage.input_tokens,
                cached_tokens=response.usage.cached_input_tokens,
                prompt_n=response.usage.prompt_processing_tokens,
            )
            await self._erase_slot(entry, lease.scope.model)
            async with self._slot_lock:
                self._slot_scopes.pop(lease.slot_id, None)
            return CacheFinalizeResult(
                retry_without_cache=allow_retry and self._config.strict_mismatch_retry,
                mismatch_reason=mismatch,
            )

        if self._should_save(lease, response):
            await self._save_slot_for_scope(
                lease.slot_id,
                lease.scope,
                entry=entry,
                response=response,
                reason="response",
            )
            await self.prune_if_due()
        return CacheFinalizeResult()

    async def abort(self, lease: LLMCacheLease) -> None:
        """Forget optimistic slot scope tracking when the LLM call failed."""
        if not lease.enabled or lease.slot_id is None:
            return
        async with self._slot_lock:
            self._slot_scopes.pop(lease.slot_id, None)

    async def prepare_uncached_retry(
        self,
        entry: QueueEntry,
        scope: LLMCacheScope,
    ) -> LLMCacheLease:
        if not self._should_use_cache(entry):
            return LLMCacheLease(enabled=False)
        assert entry.slot_id is not None
        async with self._slot_lock:
            self._slot_scopes[entry.slot_id] = scope
        log_event(
            "llm_cache_mismatch_fallback_started",
            entry.run_id or "unknown",
            user_id=entry.user_id,
            agent_id=scope.agent_id,
            slot_id=entry.slot_id,
            scope_key=scope.key,
        )
        return LLMCacheLease(enabled=True, scope=scope, slot_id=entry.slot_id)

    async def prune_if_due(self) -> None:
        if not self._config.enabled:
            return
        now = time.monotonic()
        if now - self._last_prune_at < self._config.prune_interval_seconds:
            return
        self._last_prune_at = now
        await self.prune()

    async def prune(self) -> None:
        entries = await self._store.list_all()
        active_keys: set[str]
        async with self._slot_lock:
            active_keys = {scope.key for scope in self._slot_scopes.values()}
        now = time.time()
        max_age_seconds = self._config.max_age_days * 24 * 3600
        total = sum(max(0, entry.file_size_bytes) for entry in entries)
        deleted = 0
        log_event(
            "llm_cache_prune_started",
            "unknown",
            entries=len(entries),
            total_bytes=total,
            cleanup_available=self._cleanup_available,
        )
        for entry in entries:
            if entry.scope.key in active_keys:
                continue
            too_old = now - entry.last_used_at > max_age_seconds
            over_budget = total > self._config.max_total_bytes
            if not too_old and not over_budget:
                continue
            if await self._delete_cache_entry(entry):
                total -= max(0, entry.file_size_bytes)
                deleted += 1
        log_event(
            "llm_cache_prune_finished",
            "unknown",
            deleted=deleted,
            remaining_bytes=total,
        )

    def _should_use_cache(self, entry: QueueEntry) -> bool:
        return (
            self._config.enabled
            and entry.slot_id is not None
            and entry.load_class in _SUPPORTED_LOAD_CLASSES
        )

    def _validate_reuse(
        self,
        entry: QueueEntry,
        lease: LLMCacheLease,
        response: LLMResponse,
    ) -> str | None:
        if lease.hit_kind == "none":
            return None
        prompt_tokens = response.usage.input_tokens
        if prompt_tokens <= 0:
            log_event(
                "llm_cache_restore_validation_failed",
                entry.run_id or "unknown",
                user_id=entry.user_id,
                slot_id=lease.slot_id,
                scope_key=lease.scope.key if lease.scope else "",
                reason="missing_prompt_tokens",
            )
            return None
        cached_tokens = response.usage.cached_input_tokens
        prompt_n = response.usage.prompt_processing_tokens
        reuse_ratio = cached_tokens / prompt_tokens if prompt_tokens else 0.0
        recompute_ratio = prompt_n / prompt_tokens if prompt_tokens and prompt_n else 0.0
        threshold = (
            self._config.validation_large_reuse_ratio
            if prompt_tokens >= self._config.validation_large_context_tokens
            else self._config.validation_min_reuse_ratio
        )
        log_event(
            "llm_cache_restore_validation_started",
            entry.run_id or "unknown",
            user_id=entry.user_id,
            agent_id=lease.scope.agent_id if lease.scope else "",
            slot_id=lease.slot_id,
            scope_key=lease.scope.key if lease.scope else "",
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            prompt_n=prompt_n,
            cache_reuse_ratio=round(reuse_ratio, 4),
            prompt_recompute_ratio=round(recompute_ratio, 4),
            threshold=threshold,
        )
        if reuse_ratio < threshold:
            log_event(
                "llm_cache_restore_validation_failed",
                entry.run_id or "unknown",
                user_id=entry.user_id,
                slot_id=lease.slot_id,
                scope_key=lease.scope.key if lease.scope else "",
                cache_reuse_ratio=round(reuse_ratio, 4),
                threshold=threshold,
            )
            return "low_cache_reuse_ratio"
        log_event(
            "llm_cache_restore_validation_passed",
            entry.run_id or "unknown",
            user_id=entry.user_id,
            slot_id=lease.slot_id,
            scope_key=lease.scope.key if lease.scope else "",
            cache_reuse_ratio=round(reuse_ratio, 4),
            threshold=threshold,
        )
        return None

    def _should_save(self, lease: LLMCacheLease, response: LLMResponse) -> bool:
        if self._config.save_policy == "eviction_only":
            return False
        if self._config.save_policy == "every_response":
            return True
        if response.usage.input_tokens < self._config.save_min_tokens:
            return False
        if lease.metadata is None:
            return True
        return time.time() - lease.metadata.last_saved_at >= self._config.save_dirty_seconds

    async def _save_slot_for_scope(
        self,
        slot_id: int,
        scope: LLMCacheScope,
        *,
        entry: QueueEntry,
        response: LLMResponse | None,
        reason: str,
    ) -> None:
        filename = scope.filename
        log_event(
            "llm_cache_save_started",
            entry.run_id or "unknown",
            user_id=entry.user_id,
            agent_id=scope.agent_id,
            slot_id=slot_id,
            scope_key=scope.key,
            filename=filename,
            reason=reason,
        )
        client = self._client_for(scope.provider_name)
        if client is None:
            log_event(
                "llm_cache_save_failed",
                entry.run_id or "unknown",
                user_id=entry.user_id,
                agent_id=scope.agent_id,
                slot_id=slot_id,
                scope_key=scope.key,
                filename=filename,
                reason=reason,
                error="slot_api_base_url_missing",
            )
            return
        result = await client.save(
            slot_id,
            model=scope.model,
            filename=filename,
        )
        if not result.ok:
            health.increment("llm_cache_save_failed")
            log_event(
                "llm_cache_save_failed",
                entry.run_id or "unknown",
                user_id=entry.user_id,
                agent_id=scope.agent_id,
                slot_id=slot_id,
                scope_key=scope.key,
                filename=filename,
                reason=reason,
                status_code=result.status_code,
                error=result.error,
            )
            return
        now = time.time()
        old = await self._store.get(scope.key)
        metadata = LLMCacheMetadata(
            scope=scope,
            filename=filename,
            token_count=result.n_tokens or (response.usage.input_tokens if response else 0),
            file_size_bytes=result.n_bytes or (old.file_size_bytes if old else 0),
            prompt_tokens=(
                response.usage.input_tokens if response else (old.prompt_tokens if old else 0)
            ),
            cached_tokens=(
                response.usage.cached_input_tokens
                if response
                else (old.cached_tokens if old else 0)
            ),
            prompt_n=(
                response.usage.prompt_processing_tokens
                if response
                else (old.prompt_n if old else 0)
            ),
            created_at=old.created_at if old else now,
            last_used_at=now,
            last_saved_at=now,
            save_count=(old.save_count if old else 0) + 1,
            restore_count=old.restore_count if old else 0,
        )
        await self._store.upsert(metadata)
        async with self._slot_lock:
            self._slot_scopes[slot_id] = scope
        health.increment("llm_cache_save_finished")
        log_event(
            "llm_cache_save_finished",
            entry.run_id or "unknown",
            user_id=entry.user_id,
            agent_id=scope.agent_id,
            slot_id=slot_id,
            scope_key=scope.key,
            filename=filename,
            n_saved=result.n_tokens,
            n_written=result.n_bytes,
            save_ms=round(result.server_ms or result.elapsed_ms, 3),
            reason=reason,
        )

    async def _erase_slot(self, entry: QueueEntry, model: str) -> None:
        if entry.slot_id is None:
            return
        client = self._client_for(entry.provider_name or "")
        if client is None:
            return
        result = await client.erase(entry.slot_id, model=model)
        log_event(
            "llm_cache_slot_erased",
            entry.run_id or "unknown",
            user_id=entry.user_id,
            slot_id=entry.slot_id,
            model=model,
            ok=result.ok,
            n_erased=result.n_tokens,
            error=result.error,
        )

    async def _delete_cache_entry(self, entry: LLMCacheMetadata) -> bool:
        if not self._cleanup_available:
            await self._store.delete(entry.scope.key)
            return True
        path = self._config.root_dir / entry.filename
        if not entry.filename.startswith(_MANAGED_PREFIX):
            return False
        try:
            if path.exists():
                path.unlink()
            await self._store.delete(entry.scope.key)
            return True
        except OSError as exc:
            logger.warning("Failed to delete LLM cache file %s: %s", path, exc)
            return False

    def _client_for(self, provider_name: str) -> SlotCacheClient | None:
        if self._client_override is not None:
            return self._client_override
        base_url = self._config.slot_api_base_url or self._provider_base_urls.get(provider_name)
        if not base_url:
            logger.warning("No slot API base URL for provider '%s'", provider_name)
            return None
        return LlamaSlotCacheClient(
            base_url,
            api_key=self._provider_api_keys.get(provider_name),
            timeout_seconds=self._config.http_timeout_seconds,
        )

    @staticmethod
    def _prepare_root_dir(root_dir: Path) -> bool:
        try:
            root_dir.mkdir(parents=True, exist_ok=True)
            probe = root_dir / ".corpclaw_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True
        except OSError:
            logger.warning(
                "Persistent LLM cache root is not writable: %s. "
                "save/restore may still work server-side, but local cleanup is disabled.",
                root_dir,
            )
            return False


def _metadata_from_row(row: sqlite3.Row) -> LLMCacheMetadata:
    scope = LLMCacheScope(
        user_id=str(row["user_id"]),
        conversation_id=str(row["conversation_id"]),
        agent_id=str(row["agent_id"]),
        provider_name=str(row["provider_name"]),
        model=str(row["model"]),
        preset=row["preset"],
        system_hash=str(row["system_hash"]),
        tools_hash=str(row["tools_hash"]),
    )
    return LLMCacheMetadata(
        scope=scope,
        filename=str(row["filename"]),
        token_count=int(row["token_count"]),
        file_size_bytes=int(row["file_size_bytes"]),
        prompt_tokens=int(row["prompt_tokens"]),
        cached_tokens=int(row["cached_tokens"]),
        prompt_n=int(row["prompt_n"]),
        created_at=float(row["created_at"]),
        last_used_at=float(row["last_used_at"]),
        last_saved_at=float(row["last_saved_at"]),
        save_count=int(row["save_count"]),
        restore_count=int(row["restore_count"]),
    )


def _normalize_slot_api_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3]
    return normalized.rstrip("/")


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _hash_payload(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def config_from_settings(value: Any) -> PersistentCacheConfig:
    """Build runtime config from pydantic settings without importing settings here."""
    root_dir = _resolve_path(value.root_dir)
    index_path = _resolve_path(value.index_path)
    return PersistentCacheConfig(
        enabled=bool(value.enabled),
        root_dir=root_dir,
        index_path=index_path,
        slot_api_base_url=value.slot_api_base_url,
        max_total_bytes=int(value.max_total_bytes),
        max_age_days=int(value.max_age_days),
        save_policy=value.save_policy,
        save_min_tokens=int(value.save_min_tokens),
        save_dirty_seconds=float(value.save_dirty_seconds),
        validation_min_reuse_ratio=float(value.validation_min_reuse_ratio),
        validation_large_context_tokens=int(value.validation_large_context_tokens),
        validation_large_reuse_ratio=float(value.validation_large_reuse_ratio),
        strict_mismatch_retry=bool(value.strict_mismatch_retry),
        prune_interval_seconds=float(value.prune_interval_seconds),
        http_timeout_seconds=float(value.http_timeout_seconds),
    )
