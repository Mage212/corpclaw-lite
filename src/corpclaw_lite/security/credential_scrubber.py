from __future__ import annotations

import logging
import os
import re

__all__ = [
    "CredentialScrubber",
]

logger = logging.getLogger(__name__)


class CredentialScrubber(logging.Filter):
    """
    Log filter that masks sensitive credentials.
    Pattern matches common keys: `sk-...`, `ghp_...`, Bearer tokens, AWS, Slack, URL creds.
    """

    PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"sk-[a-zA-Z0-9]{20,}"),  # OpenAI / Anthropic
        re.compile(r"ghp_[a-zA-Z0-9]{36}"),  # GitHub PAT
        re.compile(r"Bearer\s+[a-zA-Z0-9\-\._~+/]+=*"),  # Generic Bearer
        re.compile(r"AKIA[A-Z0-9]{16}"),  # AWS Access Key ID
        re.compile(r"xox[bprs]-[a-zA-Z0-9\-]+"),  # Slack tokens
        re.compile(r"://[^:\s]+:[^@\s]+@"),  # URL embedded credentials
    )

    MASK = "***REDACTED***"

    def __init__(self, name: str = "") -> None:
        super().__init__(name)
        self._patterns: list[re.Pattern[str]] = list(self.PATTERNS)
        # Dynamically scrub the IPC secret if set
        ipc_secret = os.environ.get("CORPCLAW_IPC_SECRET")
        if ipc_secret and len(ipc_secret) > 8:
            self._patterns.append(re.compile(re.escape(ipc_secret)))

    def filter(self, record: logging.LogRecord) -> bool:
        """Process the log record and scrub sensitive text."""
        if not isinstance(record.msg, str):
            # Attempt to scrub args if msg isn't standard?
            # Normally record.msg is a template string and args are applied later.
            # We must also scrub the fully formatted message.
            return True

        # Scrub the base message
        record.msg = self._scrub(record.msg)

        # Scrub string arguments
        if isinstance(record.args, tuple):
            scrubbed_args = tuple(
                self._scrub(arg) if isinstance(arg, str) else arg for arg in record.args
            )
            record.args = scrubbed_args
        elif isinstance(record.args, dict):
            scrubbed_args_dict = {
                k: self._scrub(v) if isinstance(v, str) else v for k, v in record.args.items()
            }
            record.args = scrubbed_args_dict

        return True

    def _scrub(self, text: str) -> str:
        res = text
        for pattern in self._patterns:
            res = pattern.sub(self.MASK, res)
        return res
