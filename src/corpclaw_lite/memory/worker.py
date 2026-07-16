"""B-109 / DC-027 Layer 2+3: background memory worker service.

Web-owned asyncio service that periodically refreshes each opted-in user's
long-term memory (Layer 1 ``memory_entries`` + Layer 2 ``users/{id}.md``).

Design (D1–D10 from the approved plan):
- **System maintenance**, not user consent-UI — no ``schedule_propose``.
- **One-shot ``provider.chat()``** with tools=None — not full ReAct.
- **Merge-only**: backup → write → upsert entries; never delete.
- **GPU-safe**: quiet hours + overflow (``maintenance`` load class) + busy skip.
- **Opt-in**: master switch ``enabled=false`` + per-user ``enabled=0``.
- **Backup + audit**: ``.md.bak`` always; ``log_event`` trace always.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from datetime import UTC, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from corpclaw_lite.logging.trace import log_event
from corpclaw_lite.memory.worker_merge import (
    apply_worker_entries,
    backup_user_md,
    ensure_disclaimer,
    gather_transcript,
    parse_worker_response,
    write_user_md_atomic,
)
from corpclaw_lite.memory.worker_prompt import build_system_prompt, build_user_payload

if TYPE_CHECKING:
    from corpclaw_lite.channels.user_notifier import UserNotifier
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.channels.web.chat_store import WebChatStore
    from corpclaw_lite.config.bootstrap import BootstrapLoader
    from corpclaw_lite.config.settings import MemoryWorkerSettings
    from corpclaw_lite.llm.base import Provider
    from corpclaw_lite.llm.router import LLMRouter
    from corpclaw_lite.memory.sqlite import SQLiteMemory
    from corpclaw_lite.users.manager import UserManager

logger = logging.getLogger(__name__)

__all__ = ["MemoryWorkerService"]


class MemoryWorkerService:
    """Background memory curator (web-owned; opt-in; quiet hours)."""

    def __init__(
        self,
        *,
        settings: MemoryWorkerSettings,
        user_manager: UserManager,
        memory: SQLiteMemory,
        chat_store: WebChatStore,
        context_store: ChatContextStore,
        bootstrap: BootstrapLoader,
        provider: Provider | LLMRouter | None = None,
        agent_service: Any | None = None,
        notifier: UserNotifier | None = None,
    ) -> None:
        self._settings = settings
        self._user_manager = user_manager
        self._memory = memory
        self._chat_store = chat_store
        self._context_store = context_store
        self._bootstrap = bootstrap
        self._provider = provider
        self._agent_service = agent_service
        self._notifier = notifier
        self._task: asyncio.Task[None] | None = None
        self._running = False

    # ── Lifecycle (mirrors SchedulerService) ───────────────────────────────

    def start(self) -> None:
        if not self._settings.enabled:
            logger.info("Memory worker disabled (memory_worker.enabled=false)")
            return
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self._poll_loop())
            logger.info("MemoryWorkerService started poll_seconds=%s", self._settings.poll_seconds)

    def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            logger.info("MemoryWorkerService stopped")
        self._task = None

    # ── Poll loop ──────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        interval = max(30.0, float(self._settings.poll_seconds))
        while self._running:
            try:
                if self._is_quiet_hours():
                    await self._tick()
                else:
                    logger.debug("Memory worker: outside quiet hours, sleeping")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Memory worker tick failed: %s", exc)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        """Process up to ``max_users_per_tick`` opted-in users."""
        user_ids = await self._user_manager.async_list_memory_worker_enabled_users()
        if not user_ids:
            return
        # Process oldest-first by last_run_at (None = never run → highest priority).
        sorted_ids = await self._sort_users_by_last_run(user_ids)
        cap = max(1, int(self._settings.max_users_per_tick))
        for user_id in sorted_ids[:cap]:
            try:
                user = self._user_manager.get_by_id(user_id)
                if user is None:
                    continue
                await self._run_user(user)
            except Exception as exc:
                logger.warning("Memory worker: user %s failed: %s", user_id, exc)

    async def _sort_users_by_last_run(self, user_ids: list[int]) -> list[int]:
        """Sort so never-run and oldest-run users go first."""
        scored: list[tuple[str | None, int]] = []
        for uid in user_ids:
            state = await self._user_manager.async_get_memory_worker_state(uid)
            scored.append((state.last_run_at if state else None, uid))
        scored.sort(key=lambda pair: (pair[0] or "", pair[1]))
        return [uid for _, uid in scored]

    # ── Per-user run ───────────────────────────────────────────────────────

    async def run_user(self, user: Any) -> str:
        """Process one user (public entry — used by CLI and tick).

        Returns status string: 'ok' | 'skipped' | 'error'.
        """
        return await self._run_user(user)

    async def _run_user(self, user: Any) -> str:
        """Process one user. Returns status string: 'ok' | 'skipped' | 'error'.

        This is public-ish (also called by CLI ``memory-worker run``).
        """
        user_id = int(user.id)
        mem_key = str(user.memory_key())
        # Busy gate: interactive chat always wins (DC-011 / D-084).
        acquired = False
        if self._agent_service is not None:
            try:
                ok = await self._agent_service.try_start_user_request(user_id)
            except Exception:
                ok = False
            if not ok:
                logger.debug("Memory worker: user %s busy, skipping", user_id)
                await self._user_manager.async_update_memory_worker_run(
                    user_id, status="skipped", error="user_busy"
                )
                return "skipped"
            acquired = True

        try:
            return await self._process_user_safe(user, user_id, mem_key)
        finally:
            if acquired and self._agent_service is not None:
                with contextlib.suppress(Exception):
                    await self._agent_service.finish_user_request(user_id)

    async def _process_user_safe(self, user: Any, user_id: int, mem_key: str) -> str:
        """Gather context, call LLM, merge-write. Catches all exceptions."""
        # ── Gather inputs ──────────────────────────────────────────────────
        current_md = ""
        try:
            current_md = self._bootstrap.get_user_prompt(user_id) or ""
        except Exception:
            logger.debug("Memory worker: no bootstrap .md for user %s", user_id)

        current_entries: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            current_entries = await self._memory.recall_entries(mem_key, limit=50)

        transcript = ""
        try:
            transcript = await gather_transcript(
                chat_store=self._chat_store,
                context_store=self._context_store,
                user_id=mem_key,
                max_sessions=int(self._settings.max_sessions),
                max_messages_per_session=int(self._settings.max_messages_per_session),
                max_chars=int(self._settings.max_transcript_chars),
            )
        except Exception as exc:
            logger.warning("Memory worker: gather_transcript failed for %s: %s", user_id, exc)

        # Cold-start: no data to curate.
        if not transcript and not current_md:
            logger.debug("Memory worker: user %s cold-start (no transcript, no md)", user_id)
            await self._user_manager.async_update_memory_worker_run(
                user_id, status="skipped", error="cold_start"
            )
            return "skipped"

        # ── LLM call ───────────────────────────────────────────────────────
        if self._provider is None:
            await self._user_manager.async_update_memory_worker_run(
                user_id, status="error", error="no provider configured"
            )
            return "error"

        messages = [
            {"role": "system", "content": build_system_prompt()},
            {
                "role": "user",
                "content": build_user_payload(
                    current_md=current_md,
                    current_entries=current_entries,
                    transcript=transcript,
                ),
            },
        ]

        try:
            provider = self._resolve_provider()
            timeout = max(10.0, float(getattr(self._settings, "llm_timeout_seconds", 120.0)))
            response = await asyncio.wait_for(
                provider.chat(messages=messages, tools=None),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("Memory worker: LLM timeout for user %s", user_id)
            await self._user_manager.async_update_memory_worker_run(
                user_id, status="error", error="llm_timeout"
            )
            return "error"
        except Exception as exc:
            logger.warning("Memory worker: LLM call failed for user %s: %s", user_id, exc)
            await self._user_manager.async_update_memory_worker_run(
                user_id, status="error", error=str(exc)[:500]
            )
            return "error"

        raw = response.content or ""

        # ── Parse response ─────────────────────────────────────────────────
        update = parse_worker_response(raw, max_entries=int(self._settings.max_entries_per_run))
        if update is None:
            logger.warning("Memory worker: bad JSON from LLM for user %s", user_id)
            await self._user_manager.async_update_memory_worker_run(
                user_id, status="error", error="bad_llm_json"
            )
            return "error"

        # ── Merge-write ────────────────────────────────────────────────────
        md_changed = self._write_md(user_id, update.md)
        entries_applied = await apply_worker_entries(self._memory, mem_key, update.entries)

        await self._user_manager.async_update_memory_worker_run(user_id, status="ok")
        log_event(
            "memory_worker_run",
            "",
            user_id=user_id,
            status="ok",
            md_changed=md_changed,
            entries_applied=entries_applied,
            summary=update.summary[:200],
        )

        # Optional user-visible notify.
        notifier = self._notifier
        should_notify = (
            self._settings.notify_on_update
            and notifier is not None
            and (md_changed or entries_applied)
        )
        if should_notify and notifier is not None:
            text = (
                f"Обновил долговременную память: {update.summary}"
                if update.summary
                else "Обновил долговременную память."
            )
            with contextlib.suppress(Exception):
                await notifier.notify(
                    user,
                    text,
                    source="memory_worker",
                    persist=False,
                )

        return "ok"

    # ── Helpers ────────────────────────────────────────────────────────────

    def _resolve_provider(self) -> Any:
        """Resolve provider for memory_worker task_kind (maintenance/overflow)."""
        from corpclaw_lite.llm.router import LLMRouter

        provider = self._provider
        if isinstance(provider, LLMRouter):
            try:
                return provider.for_task("memory_worker")
            except Exception:
                pass
        return provider

    def _write_md(self, user_id: int, new_md: str) -> bool:
        """Backup + write the user .md. Returns True if content changed."""
        md_path = self._resolve_user_md_path(user_id)
        if md_path is None:
            logger.debug("Memory worker: no .md path for user %s, skipping md write", user_id)
            return False
        content = ensure_disclaimer(new_md)
        old_hash = ""
        if md_path.exists():
            old_hash = hashlib.sha256(md_path.read_bytes()).hexdigest()
        backup_user_md(md_path, keep_history=bool(self._settings.keep_history_backups))
        new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if old_hash == new_hash:
            return False
        write_user_md_atomic(md_path, content)
        return True

    def _resolve_user_md_path(self, user_id: int) -> Path | None:
        """Find the .md path by checking bootstrap dirs (overlay-aware)."""
        dirs = getattr(self._bootstrap, "_dirs", None)
        if not dirs:
            return None
        for directory in reversed(dirs):
            candidate = directory / "users" / f"{user_id}.md"
            if candidate.parent.exists():
                return candidate
        return None

    # ── Quiet hours ────────────────────────────────────────────────────────

    def _is_quiet_hours(self, *, now: datetime | None = None) -> bool:
        """Check if *now* falls within the configured quiet hours window."""
        now = now or datetime.now(self._tz())
        start = self._parse_hhmm(self._settings.quiet_hours_start)
        end = self._parse_hhmm(self._settings.quiet_hours_end)
        if start is None or end is None:
            # If unparseable, treat as always-quiet (fail-open for maintenance).
            return True
        current = now.time()
        if start <= end:
            # Same-day window (e.g. 09:00–17:00).
            return start <= current <= end
        # Wraps midnight (e.g. 22:00–07:00).
        return current >= start or current <= end

    def _tz(self) -> Any:
        import zoneinfo

        tz_name = self._settings.timezone
        if not tz_name:
            tz_name = "UTC"
        try:
            return zoneinfo.ZoneInfo(tz_name)
        except Exception:
            return UTC

    @staticmethod
    def _parse_hhmm(raw: str) -> time | None:
        try:
            parts = raw.strip().split(":")
            return time(hour=int(parts[0]), minute=int(parts[1]))
        except (ValueError, IndexError):
            return None
