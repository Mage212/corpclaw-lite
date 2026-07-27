from __future__ import annotations

import logging
import os
import re

__all__ = [
    "CredentialScrubbingFormatter",
    "CredentialScrubber",
    "scrub_text",
]

logger = logging.getLogger(__name__)


def scrub_text(text: str) -> str:
    """Scrub known credential patterns from arbitrary text.

    Used to sanitise tool results before they enter the LLM context,
    user responses, or memory storage — paths not covered by the
    logging.Filter-based CredentialScrubber.
    """
    result = text
    for pattern in CredentialScrubber.PATTERNS:
        result = pattern.sub(CredentialScrubber.MASK, result)
    ipc_secret = os.environ.get("CORPCLAW_IPC_SECRET")
    if ipc_secret and len(ipc_secret) > 8:
        result = result.replace(ipc_secret, CredentialScrubber.MASK)
    return result


class CredentialScrubber(logging.Filter):
    """
    Log filter that masks sensitive credentials.
    Pattern matches common keys: `sk-...`, `ghp_...`, Bearer tokens, AWS, Slack, URL creds.
    """

    PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"sk-(?:proj|ant|svcacct)-[a-zA-Z0-9_-]{16,}"),
        re.compile(r"sk-[a-zA-Z0-9_-]{20,}"),  # OpenAI-compatible legacy keys
        re.compile(r"\bhf_[a-zA-Z0-9]{20,}"),  # Hugging Face
        re.compile(r"\bglpat-[a-zA-Z0-9_-]{16,}"),  # GitLab PAT
        re.compile(r"\bgithub_pat_[a-zA-Z0-9_]{20,}"),  # GitHub fine-grained PAT
        re.compile(r"\bbot\d{6,}:[A-Za-z0-9_-]{20,}"),  # Telegram bot token in URLs
        re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}"),  # Raw Telegram bot token
        re.compile(r"ghp_[a-zA-Z0-9]{20,}"),  # GitHub PAT (sync with tool_guard_rules)
        re.compile(r"Bearer\s+[a-zA-Z0-9\-\._~+/]+=*"),  # Generic Bearer
        re.compile(r"AKIA[A-Z0-9]{16}"),  # AWS Access Key ID
        re.compile(r"xox[bprs]-[a-zA-Z0-9\-]+"),  # Slack tokens
        re.compile(r"://[^:\s]+:[^@\s]+@"),  # URL embedded credentials
        re.compile(r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"),  # PEM private keys
        # H-7 (code review): additional credential formats previously missed.
        re.compile(r"AIza[0-9A-Za-z_-]{35}"),  # Google API key (39 chars total)
        re.compile(  # JWT (three base64url segments separated by dots)
            r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
        ),
        # HTTP auth headers carrying a raw token (case-insensitive header name).
        # `Authorization: Bearer ...` is covered by the Bearer pattern above;
        # these catch non-Bearer schemes (Token, Basic, custom) and x-api-key.
        # The optional scheme word (e.g. "Token", "Basic") is skipped before the
        # token so "authorization: Token <token>" still matches.
        re.compile(r"(?i)x-api-key:\s*[A-Za-z0-9_.-]{20,}"),
        re.compile(r"(?i)authorization:\s*(?:[A-Za-z][A-Za-z0-9]*\s+)?[A-Za-z0-9_.-]{20,}"),
    )

    MASK = "***REDACTED***"

    def __init__(self, name: str = "") -> None:
        super().__init__(name)
        # B-074/L3: only static patterns here. The IPC secret is re-read from
        # the environment on each filter() call (see _scrub) so a secret that
        # is rotated or loaded from .env after logging setup is still redacted
        # — matching the module-level scrub_text() behaviour. Caching it here
        # (the previous behaviour) leaked the secret into logs in that window.
        self._patterns: list[re.Pattern[str]] = list(self.PATTERNS)

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

        # Scrub exception traceback text (credentials can leak via logger.exception())
        if record.exc_text:
            record.exc_text = self._scrub(record.exc_text)

        return True

    def _scrub(self, text: str) -> str:
        res = text
        for pattern in self._patterns:
            res = pattern.sub(self.MASK, res)
        # B-074/L3: dynamically scrub the IPC secret on each call, matching
        # scrub_text(). Cheap (a single str.replace) and rotation-safe.
        ipc_secret = os.environ.get("CORPCLAW_IPC_SECRET")
        if ipc_secret and len(ipc_secret) > 8:
            res = res.replace(ipc_secret, self.MASK)
        return res


class CredentialScrubbingFormatter(logging.Formatter):
    """Scrub the final formatted record, including formatter-created traceback text."""

    def format(self, record: logging.LogRecord) -> str:
        return scrub_text(super().format(record))
