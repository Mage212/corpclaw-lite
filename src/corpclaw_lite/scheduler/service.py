"""B-118 / DC-030: SchedulerService — poll loop + consent CRUD + headless dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from corpclaw_lite.scheduler.models import ScheduledTask, ScheduleSpec
from corpclaw_lite.scheduler.parse import (
    ScheduleParseError,
    compute_next_run,
    parse_schedule,
)
from corpclaw_lite.scheduler.prompt import build_headless_prompt
from corpclaw_lite.scheduler.store import InsertConflict, SchedulerStore

if TYPE_CHECKING:
    from corpclaw_lite.channels.service import AgentRequestService
    from corpclaw_lite.channels.user_notifier import UserNotifier
    from corpclaw_lite.config.settings import SchedulerSettings
    from corpclaw_lite.llm.base import Provider
    from corpclaw_lite.scheduler.parse_assist import ParseAssistResult
    from corpclaw_lite.users.manager import UserManager
    from corpclaw_lite.users.models import User

__all__ = [
    "SchedulerError",
    "SchedulerService",
    "make_dedup_key",
]

logger = logging.getLogger(__name__)


class SchedulerError(Exception):
    """Domain error for scheduler operations (limits, not found, bad state)."""


def make_dedup_key(*, user_id: int, title: str, task_text: str, schedule_text: str) -> str:
    raw = (
        f"{user_id}|{title.strip().lower()}|"
        f"{task_text.strip().lower()}|{schedule_text.strip().lower()}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


class SchedulerService:
    """Web-owned poll loop; consent-first task lifecycle."""

    def __init__(
        self,
        *,
        store: SchedulerStore,
        user_manager: UserManager,
        agent_service: AgentRequestService | None = None,
        notifier: UserNotifier | None = None,
        settings: SchedulerSettings,
        provider: Provider | None = None,
    ) -> None:
        self._store = store
        self._user_manager = user_manager
        self._agent_service = agent_service
        self._notifier = notifier
        self._settings = settings
        # B-143 PR3: optional LLM for parse-assist (never used on poll).
        self._provider = provider
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def store(self) -> SchedulerStore:
        return self._store

    def start(self) -> None:
        if not self._settings.enabled:
            logger.info("Scheduler disabled (scheduler.enabled=false)")
            return
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self._poll_loop())
            logger.info(
                "SchedulerService started poll_seconds=%s db=%s",
                self._settings.poll_seconds,
                self._store.db_path,
            )

    def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            logger.info("SchedulerService stopped")
        self._task = None

    async def _poll_loop(self) -> None:
        interval = max(5.0, float(self._settings.poll_seconds))
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Scheduler tick failed: %s", exc)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    def _stale_before(self, now: datetime) -> str:
        ttl = max(60.0, float(self._settings.claim_ttl_seconds))
        return _iso(now - timedelta(seconds=ttl))

    async def tick(self) -> None:
        """One poll cycle: expire pending, run due tasks."""
        await self._expire_pending()
        now = _utcnow()
        due = await self._store.list_due(
            _iso(now), stale_before_iso=self._stale_before(now), limit=20
        )
        for task in due:
            try:
                await self._dispatch(task, now=now)
            except Exception as exc:
                logger.exception("Dispatch failed task=%s: %s", task.id, exc)
                task.error_count += 1
                task.last_status = "error"
                task.last_run_at = _iso(now)
                task.claimed_at = None
                task.claim_token = None
                # Defer 1 min to avoid tight error loop
                task.next_run_at = _iso(now + timedelta(minutes=1))
                await self._store.update(task)

    async def _expire_pending(self) -> None:
        days = int(self._settings.pending_ttl_days)
        if days <= 0:
            return
        cutoff = _iso(_utcnow() - timedelta(days=days))
        n = await self._store.expire_pending(cutoff)
        if n:
            logger.info("Expired %d pending schedule proposals", n)

    async def propose(
        self,
        user: User,
        *,
        title: str,
        task_text: str,
        schedule_text: str,
        dedup_key: str | None = None,
        notify: bool = True,
    ) -> ScheduledTask:
        title_s = (title or "").strip() or "Задача по расписанию"
        task_s = (task_text or "").strip()
        sched_s = (schedule_text or "").strip()
        if not task_s:
            raise SchedulerError("task_text is required")
        if not sched_s:
            raise SchedulerError("schedule_text is required")
        max_task = int(self._settings.max_task_text_chars)
        max_sched = int(self._settings.max_schedule_text_chars)
        if len(task_s) > max_task:
            raise SchedulerError(f"task_text exceeds {max_task} characters")
        if len(sched_s) > max_sched:
            raise SchedulerError(f"schedule_text exceeds {max_sched} characters")

        key = dedup_key or make_dedup_key(
            user_id=user.id, title=title_s, task_text=task_s, schedule_text=sched_s
        )

        tz = self._settings.timezone
        try:
            spec = parse_schedule(sched_s, now=_utcnow(), tz=tz)
        except ScheduleParseError:
            spec = ScheduleSpec(kind="unset")

        task = ScheduledTask(
            id=uuid.uuid4().hex,
            user_id=user.id,
            title=title_s[:120],
            task_text=task_s,
            schedule_text=sched_s,
            schedule=spec,
            timezone=tz,
            status="pending",
            enabled=True,
            dedup_key=key,
            next_run_at=None,
        )
        try:
            await self._store.insert_pending_if_allowed(
                task, max_n=int(self._settings.max_tasks_per_user)
            )
        except InsertConflict as exc:
            raise SchedulerError(str(exc)) from exc

        if notify and self._notifier is not None:
            msg = self._format_propose_message(task)
            preview = task.task_text
            max_preview = 500
            if len(preview) > max_preview:
                preview = preview[: max_preview - 1] + "…"
            try:
                await self._notifier.notify(
                    user,
                    msg,
                    source="schedule_propose",
                    title=f"Подтвердите: {task.title}",
                    extra_metadata={
                        "kind": "schedule_confirm",
                        "task_id": task.id,
                        "title": task.title,
                        "task_text": preview,
                        "schedule_text": task.schedule_text,
                        "schedule_kind": task.schedule.kind,
                        "timezone": task.timezone,
                        "status": task.status,
                    },
                )
            except Exception as exc:
                logger.warning("schedule propose notify failed: %s", exc)

        return task

    def _format_propose_message(self, task: ScheduledTask) -> str:
        # B-143: primary path is system-inbox card + «Задачи»; CLI remains for ops.
        parse_note = (
            f"Разбор: {task.schedule.kind}"
            if task.schedule.kind != "unset"
            else "Разбор: не распознано (при подтверждении укажите формулу в «Задачи»)"
        )
        return (
            f"Предложена задача по расписанию (ожидает подтверждения).\n\n"
            f"Название: {task.title}\n"
            f"Когда: {task.schedule_text}\n"
            f"{parse_note}\n"
            f"Часовой пояс: {task.timezone}\n\n"
            f"Подтвердите или отклоните кнопками ниже, либо откройте «Задачи».\n"
            f"(CLI: schedule accept/dismiss -u {task.user_id} -i {task.id})\n"
        )

    async def parse_assist(
        self,
        user: User,
        task_id: str,
        *,
        schedule_text: str | None = None,
    ) -> ParseAssistResult:
        """B-143: one LLM call to map free-form schedule_text → validated formula.

        Does not activate the task. Human must still call :meth:`accept`.
        """
        from corpclaw_lite.scheduler.parse_assist import ParseAssistError, llm_parse_schedule

        task = await self._store.get(task_id, user_id=user.id)
        if task is None:
            raise SchedulerError(f"Task {task_id} not found")
        if task.status != "pending":
            raise SchedulerError(f"Task is not pending (status={task.status})")

        text = (
            schedule_text.strip()
            if isinstance(schedule_text, str) and schedule_text.strip()
            else task.schedule_text
        )
        # If deterministic parse already works, skip LLM.
        try:
            spec = parse_schedule(text, now=_utcnow(), tz=task.timezone)
            if spec.kind != "unset":
                next_run = compute_next_run(spec, last_run_at=None, now=_utcnow(), tz=task.timezone)
                from corpclaw_lite.scheduler.parse_assist import ParseAssistResult

                return ParseAssistResult(
                    formula=text,
                    explanation="Распознано без LLM (детерминистический разбор).",
                    schedule=spec,
                    next_run_at=_iso(next_run) if next_run else None,
                    original_text=text,
                )
        except ScheduleParseError:
            pass

        provider = self._provider
        if provider is None and self._agent_service is not None:
            loop = getattr(getattr(self._agent_service, "_stack", None), "loop", None)
            provider = getattr(loop, "provider", None) if loop is not None else None
        if provider is None:
            raise SchedulerError("LLM parse-assist is not available (no provider configured).")

        try:
            return await llm_parse_schedule(
                provider,
                schedule_text=text,
                timezone=task.timezone,
                now=_utcnow(),
            )
        except ParseAssistError as exc:
            raise SchedulerError(str(exc)) from exc

    async def accept(
        self,
        user: User,
        task_id: str,
        *,
        title: str | None = None,
        task_text: str | None = None,
        schedule_text: str | None = None,
    ) -> ScheduledTask:
        task = await self._store.get(task_id, user_id=user.id)
        if task is None:
            raise SchedulerError(f"Task {task_id} not found")
        if task.status != "pending":
            raise SchedulerError(f"Task is not pending (status={task.status})")

        if title is not None and title.strip():
            task.title = title.strip()[:120]
        if task_text is not None and task_text.strip():
            text = task_text.strip()
            max_task = int(self._settings.max_task_text_chars)
            if len(text) > max_task:
                raise SchedulerError(f"task_text exceeds {max_task} characters")
            task.task_text = text
        if schedule_text is not None and schedule_text.strip():
            sched = schedule_text.strip()
            max_sched = int(self._settings.max_schedule_text_chars)
            if len(sched) > max_sched:
                raise SchedulerError(f"schedule_text exceeds {max_sched} characters")
            task.schedule_text = sched

        try:
            task.schedule = parse_schedule(task.schedule_text, now=_utcnow(), tz=task.timezone)
        except ScheduleParseError as exc:
            raise SchedulerError(str(exc)) from exc

        next_run = compute_next_run(
            task.schedule, last_run_at=None, now=_utcnow(), tz=task.timezone
        )
        if next_run is None:
            raise SchedulerError("Could not compute next_run for schedule")

        task.status = "active"
        task.enabled = True
        task.next_run_at = _iso(next_run)
        task.accepted_at = _iso(_utcnow())
        ok = await self._store.update_guarded(task, expected_status="pending")
        if not ok:
            raise SchedulerError(
                "Task was modified concurrently (expected pending); reload the task list."
            )

        if self._notifier is not None:
            try:
                await self._notifier.notify(
                    user,
                    (
                        f"Задача по расписанию активна.\n"
                        f"ID: {task.id}\n"
                        f"{task.title}\n"
                        f"Следующий запуск (UTC): {task.next_run_at}"
                    ),
                    source="schedule_accept",
                    title=task.title,
                )
            except Exception as exc:
                logger.warning("schedule accept notify failed: %s", exc)
        return task

    async def dismiss(self, user: User, task_id: str) -> ScheduledTask:
        task = await self._require(user, task_id)
        if task.status not in {"pending", "active", "paused"}:
            raise SchedulerError(f"Cannot dismiss status={task.status}")
        task.status = "dismissed"
        task.enabled = False
        task.next_run_at = None
        ok = await self._store.update_guarded(task, expected_status=("pending", "active", "paused"))
        if not ok:
            raise SchedulerError(
                "Task was modified concurrently (expected pending/active/paused); "
                "reload the task list."
            )
        return task

    async def pause(self, user: User, task_id: str) -> ScheduledTask:
        task = await self._require(user, task_id)
        if task.status != "active":
            raise SchedulerError("Only active tasks can be paused")
        task.status = "paused"
        task.enabled = False
        ok = await self._store.update_guarded(task, expected_status="active")
        if not ok:
            raise SchedulerError(
                "Task was modified concurrently (expected active); reload the task list."
            )
        return task

    async def resume(self, user: User, task_id: str) -> ScheduledTask:
        task = await self._require(user, task_id)
        if task.status != "paused":
            raise SchedulerError("Only paused tasks can be resumed")
        next_run = compute_next_run(
            task.schedule,
            last_run_at=_parse_iso(task.last_run_at),
            now=_utcnow(),
            tz=task.timezone,
        )
        task.status = "active"
        task.enabled = True
        task.next_run_at = _iso(next_run) if next_run else _iso(_utcnow())
        ok = await self._store.update_guarded(task, expected_status="paused")
        if not ok:
            raise SchedulerError(
                "Task was modified concurrently (expected paused); reload the task list."
            )
        return task

    async def delete(self, user: User, task_id: str) -> None:
        task = await self._require(user, task_id)
        task.status = "dismissed"
        task.enabled = False
        task.next_run_at = None
        await self._store.update(task)

    async def list_for_user(
        self, user: User, *, statuses: list[str] | None = None
    ) -> list[ScheduledTask]:
        return await self._store.list_for_user(user.id, statuses=statuses)

    async def run_now(self, user: User, task_id: str) -> dict[str, Any]:
        task = await self._require(user, task_id)
        if task.status not in {"active", "paused"}:
            raise SchedulerError("run-now only for active/paused tasks")
        # Ensure claim path can match next_run_at (paused may have future next_run).
        if task.status == "paused":
            task.status = "active"
            task.enabled = True
            if task.next_run_at is None:
                task.next_run_at = _iso(_utcnow())
            await self._store.update(task)
        elif task.next_run_at is None or task.next_run_at > _iso(_utcnow()):
            # Force due so claim can match current next_run after bump.
            task.next_run_at = _iso(_utcnow())
            task.enabled = True
            await self._store.update(task)
            # re-load for claim concurrency key
            refreshed = await self._store.get(task.id, user_id=user.id)
            if refreshed is not None:
                task = refreshed
        return await self._dispatch(task, now=_utcnow())

    async def _require(self, user: User, task_id: str) -> ScheduledTask:
        task = await self._store.get(task_id, user_id=user.id)
        if task is None:
            raise SchedulerError(f"Task {task_id} not found")
        return task

    async def _dispatch(
        self,
        task: ScheduledTask,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        if self._agent_service is None:
            raise SchedulerError("agent_service is not configured")
        if task.next_run_at is None:
            return {"status": "not_claimed", "reason": "no_next_run", "task_id": task.id}

        claim_token = uuid.uuid4().hex
        now_iso = _iso(now)
        claimed = await self._store.claim_task(
            task.id,
            expected_next_run_at=task.next_run_at,
            now_iso=now_iso,
            claim_token=claim_token,
            stale_before_iso=self._stale_before(now),
        )
        if not claimed:
            return {"status": "not_claimed", "task_id": task.id}

        user = self._user_manager.get_by_id(task.user_id)
        if user is None:
            task.last_status = "error"
            task.error_count += 1
            task.claimed_at = None
            task.claim_token = None
            task.next_run_at = _iso(now + timedelta(minutes=5))
            await self._store.update(task)
            return {"status": "error", "reason": "user_not_found"}

        from zoneinfo import ZoneInfo

        try:
            zone = ZoneInfo(task.timezone)
        except Exception:
            zone = ZoneInfo("UTC")
        now_local = now.astimezone(zone)
        prompt = build_headless_prompt(
            task_text=task.task_text,
            schedule_text=task.schedule_text,
            now_local=now_local,
            tz=task.timezone,
        )

        try:
            result = await self._agent_service.run_headless(
                user=user,
                task=prompt,
                source="scheduled",
            )
        except Exception:
            task.error_count += 1
            task.last_status = "error"
            task.last_run_at = _iso(now)
            task.claimed_at = None
            task.claim_token = None
            task.next_run_at = _iso(now + timedelta(minutes=1))
            await self._store.update(task)
            raise

        if result.status == "skipped":
            # Defer 1 minute to avoid tight busy loop (plan).
            task.last_status = "skipped_busy"
            task.last_run_at = _iso(now)
            task.next_run_at = _iso(now + timedelta(minutes=1))
            task.claimed_at = None
            task.claim_token = None
            await self._store.update(task)
            await self._store.append_run_log(
                task_id=task.id,
                status="skipped_busy",
                skip_reason=result.skip_reason,
            )
            return {
                "status": "skipped",
                "skip_reason": result.skip_reason,
                "task_id": task.id,
            }

        # completed
        task.run_count += 1
        task.last_run_at = _iso(now)
        task.last_status = "ok"
        task.claimed_at = None
        task.claim_token = None
        reply = result.reply or ""
        run_id = result.stats.run_id if result.stats is not None else None

        if task.schedule.kind == "once":
            task.status = "done"
            task.enabled = False
            task.next_run_at = None
        else:
            nxt = compute_next_run(
                task.schedule,
                last_run_at=now,
                now=now,
                tz=task.timezone,
            )
            if nxt is None:
                task.status = "done"
                task.enabled = False
                task.next_run_at = None
            else:
                # Ensure strictly after now for intervals
                if nxt <= now:
                    nxt = now + timedelta(minutes=max(1, task.schedule.minutes or 1))
                task.next_run_at = _iso(nxt)

        await self._store.update(task)
        await self._store.append_run_log(
            task_id=task.id,
            status="ok",
            run_id=run_id,
            reply_preview=reply[:500],
        )
        return {
            "status": "completed",
            "task_id": task.id,
            "reply": reply,
            "next_run_at": task.next_run_at,
            "task_status": task.status,
        }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None
