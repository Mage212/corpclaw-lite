__all__ = [
    "CB_DELETE_BACK",
    "CB_DELETE_CANCEL",
    "CB_DELETE_CONFIRM",
    "CB_DELETE_DIR",
    "CB_DELETE_EXEC",
    "CB_DELETE_FILE",
    "CB_DELETE_NOOP",
    "CB_DELETE_PAGE",
    "CB_DELETE_REFRESH",
    "CB_DELETE_ROOT",
    "CB_DELETE_UP",
    "CB_SCHED_ACCEPT",
    "CB_SCHED_DISMISS",
    "CB_SCHED_PREFIX",
    "parse_schedule_callback",
    "schedule_accept_data",
    "schedule_dismiss_data",
]

"""Callback data prefixes for Telegram inline workflows."""

# Delete file manager flow
CB_DELETE_PAGE = "del:page:"
CB_DELETE_DIR = "del:dir:"
CB_DELETE_FILE = "del:file:"
CB_DELETE_UP = "del:up"
CB_DELETE_ROOT = "del:root"
CB_DELETE_REFRESH = "del:refresh"
CB_DELETE_BACK = "del:back"
CB_DELETE_CONFIRM = "del:confirm"
CB_DELETE_CANCEL = "del:cancel"
CB_DELETE_EXEC = "del:exec"
CB_DELETE_NOOP = "del:noop"

# B-143 PR2: schedule consent (task_id is 32-char hex → total ≤ 37 < 64)
CB_SCHED_PREFIX = "sc:"
CB_SCHED_ACCEPT = "sc:a:"
CB_SCHED_DISMISS = "sc:d:"


def schedule_accept_data(task_id: str) -> str:
    return f"{CB_SCHED_ACCEPT}{task_id}"


def schedule_dismiss_data(task_id: str) -> str:
    return f"{CB_SCHED_DISMISS}{task_id}"


def parse_schedule_callback(data: str) -> tuple[str, str] | None:
    """Return ``(action, task_id)`` where action is ``accept`` or ``dismiss``."""
    if data.startswith(CB_SCHED_ACCEPT):
        task_id = data[len(CB_SCHED_ACCEPT) :].strip()
        if task_id:
            return ("accept", task_id)
    if data.startswith(CB_SCHED_DISMISS):
        task_id = data[len(CB_SCHED_DISMISS) :].strip()
        if task_id:
            return ("dismiss", task_id)
    return None
