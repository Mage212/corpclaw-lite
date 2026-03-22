import pytest
from pathlib import Path

from corpclaw_lite.channels.cli import CLIChannel
from corpclaw_lite.users.models import User

@pytest.fixture
def test_user():
    return User(
        id=1,
        name="Test User",
        telegram_id=123456789,
        department="IT",
    )

@pytest.mark.asyncio
async def test_cli_channel(capsys: pytest.CaptureFixture[str], test_user: User) -> None:
    channel = CLIChannel()
    await channel.start()
    out, err = capsys.readouterr()
    assert "CLI Channel started" in out

    await channel.send_message(test_user, "Hello World")
    out, err = capsys.readouterr()
    assert "Hello World" in out
    assert "Test User" in out

    await channel.send_file(test_user, Path("test.txt"), "caption")
    out, err = capsys.readouterr()
    assert "Sending file to Test User" in out
    assert "test.txt" in out
    assert "caption" in out

    await channel.stop()
    out, err = capsys.readouterr()
    assert "CLI Channel stopped" in out
