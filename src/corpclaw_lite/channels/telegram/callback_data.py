__all__ = [
    "CB_APPROVE",
    "CB_DELETE_BACK",
    "CB_DELETE_CANCEL",
    "CB_DELETE_CONFIRM",
    "CB_DELETE_DIR",
    "CB_DELETE_EXEC",
    "CB_DELETE_FILE",
    "CB_DELETE_NOOP",
    "CB_DELETE_OPEN",
    "CB_DELETE_PAGE",
    "CB_DELETE_REFRESH",
    "CB_DELETE_ROOT",
    "CB_DELETE_UP",
    "CB_DENY",
]

"""Callback data prefixes for Telegram inline workflows."""

# Delete file manager flow
CB_DELETE_OPEN = "del:open"
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

# Approval flow (used by ToolGuard)
CB_APPROVE = "appr:yes:"
CB_DENY = "appr:no:"
