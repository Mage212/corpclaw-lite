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
    "CB_FB_DOWN",
    "CB_FB_PREFIX",
    "CB_FB_UP",
    "CB_SCHED_ACCEPT",
    "CB_SCHED_DISMISS",
    "CB_SCHED_PREFIX",
    "feedback_down_data",
    "feedback_up_data",
    "parse_feedback_callback",
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

# B-121: user feedback 👍/👎 on assistant runs. Stateless callback_data carries
# the run_id (32-char hex, uuid4().hex) — total ≤ 37 < 64-byte Telegram limit.
# Stateless because the run_id is encoded in the button itself, so the mapping
# survives restarts (unlike the in-memory _pending_approvals dict).
CB_FB_PREFIX = "fb:"
CB_FB_UP = "fb:up:"
CB_FB_DOWN = "fb:down:"


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


def feedback_up_data(run_id: str) -> str:
    return f"{CB_FB_UP}{run_id}"


def feedback_down_data(run_id: str) -> str:
    return f"{CB_FB_DOWN}{run_id}"


def parse_feedback_callback(data: str) -> tuple[str, str] | None:
    """Return ``(rating, run_id)`` where rating is ``up`` or ``down``."""
    if data.startswith(CB_FB_UP):
        run_id = data[len(CB_FB_UP) :].strip()
        if run_id:
            return ("up", run_id)
    if data.startswith(CB_FB_DOWN):
        run_id = data[len(CB_FB_DOWN) :].strip()
        if run_id:
            return ("down", run_id)
    return None
