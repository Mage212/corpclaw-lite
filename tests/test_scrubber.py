import logging
from corpclaw_lite.security.credential_scrubber import CredentialScrubber

def test_credential_scrubber():
    scrubber = CredentialScrubber()
    
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="",
        lineno=0, msg="Using API key sk-12345678901234567890abc", args=(), exc_info=None
    )
    
    scrubber.filter(record)
    assert "sk-" not in record.msg
    assert "***REDACTED***" in record.msg
    
    # Test args
    record_args = logging.LogRecord(
        name="test", level=logging.INFO, pathname="",
        lineno=0, msg="Key: %s", args=("ghp_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8",), exc_info=None
    )
    scrubber.filter(record_args)
    assert record_args.args[0] == "***REDACTED***"
