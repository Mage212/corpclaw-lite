import logging
import re

logger = logging.getLogger(__name__)


class CredentialScrubber(logging.Filter):
    """
    Log filter that masks sensitive credentials.
    Pattern matches common keys: `sk-...`, `ghp_...`, Bearer tokens.
    """
    
    PATTERNS: list[re.Pattern] = [
        re.compile(r"sk-[a-zA-Z0-9]{20,}"),  # OpenAI / Anthropic
        re.compile(r"ghp_[a-zA-Z0-9]{36}"),  # GitHub PAT
        re.compile(r"Bearer\s+[a-zA-Z0-9\-\._~+/]+=*"),  # Generic Bearer
    ]
    
    MASK = "***REDACTED***"

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
                self._scrub(arg) if isinstance(arg, str) else arg 
                for arg in record.args
            )
            record.args = scrubbed_args
        elif isinstance(record.args, dict):
            scrubbed_args_dict = {
                k: self._scrub(v) if isinstance(v, str) else v
                for k, v in record.args.items()
            }
            record.args = scrubbed_args_dict
            
        return True
        
    def _scrub(self, text: str) -> str:
        res = text
        for pattern in self.PATTERNS:
            res = pattern.sub(self.MASK, res)
        return res
