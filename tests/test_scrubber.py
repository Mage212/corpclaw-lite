import logging

from corpclaw_lite.security.credential_scrubber import CredentialScrubber, scrub_text


def test_credential_scrubber():
    scrubber = CredentialScrubber()

    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Using API key sk-12345678901234567890abc",
        args=(),
        exc_info=None,
    )

    scrubber.filter(record)
    assert "sk-" not in record.msg
    assert "***REDACTED***" in record.msg

    # Test args
    record_args = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Key: %s",
        args=("ghp_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8",),
        exc_info=None,
    )
    scrubber.filter(record_args)
    assert record_args.args[0] == "***REDACTED***"


def test_scrub_text_removes_openai_key():
    """scrub_text() must redact credentials from arbitrary strings (e.g. tool results)."""
    text = "Here is your key: sk-12345678901234567890abc and some other content."
    result = scrub_text(text)
    assert "sk-" not in result
    assert "***REDACTED***" in result
    assert "other content" in result  # non-sensitive parts preserved


def test_scrub_text_removes_github_pat():
    raw = "token=ghp_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8"
    result = scrub_text(raw)
    assert "ghp_" not in result
    assert "***REDACTED***" in result


def test_scrub_text_removes_telegram_bot_token():
    raw = "https://api.telegram.org/bot8395052378:AAG_4qzvUHcm6ud0_h4r--W17CxCRX-mkK8/getMe"
    result = scrub_text(raw)
    assert "8395052378:AAG_" not in result
    assert "bot***REDACTED***" not in result
    assert "***REDACTED***" in result
    assert "https://api.telegram.org/" in result


def test_credential_scrubber_removes_telegram_token_in_args():
    scrubber = CredentialScrubber()
    token = "8395052378:AAG_4qzvUHcm6ud0_h4r--W17CxCRX-mkK8"
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Bot URL: %s",
        args=(f"https://api.telegram.org/bot{token}/sendMessage",),
        exc_info=None,
    )

    scrubber.filter(record)

    assert token not in record.args[0]
    assert "***REDACTED***" in record.args[0]


def test_scrub_text_clean_string_unchanged():
    clean = "The result is 42, no secrets here."
    assert scrub_text(clean) == clean


def test_credential_scrubber_exc_text():
    """P1-1: Credentials in exception tracebacks must be scrubbed."""
    scrubber = CredentialScrubber()

    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg="Connection failed",
        args=(),
        exc_info=None,
    )
    # Simulate formatted traceback containing a secret
    record.exc_text = (
        "Traceback (most recent call last):\n"
        '  File "auth.py", line 42, in connect\n'
        "    raise AuthError(f'key={sk_secret}')\n"
        "AuthError: key=sk-12345678901234567890abc"
    )

    scrubber.filter(record)
    assert "sk-" not in record.exc_text
    assert "***REDACTED***" in record.exc_text
