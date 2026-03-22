import pytest
from pathlib import Path

from corpclaw_lite.channels.cli import CLIChannel


@pytest.mark.asyncio
async def test_cli_channel(capsys: pytest.CaptureFixture[str]) -> None:
    channel = CLIChannel()
    await channel.start()
    out, err = capsys.readouterr()
    assert "CLI Channel started" in out

    await channel.send_message("test", "Hello World")
    out, err = capsys.readouterr()
    assert "Hello World" in out

    await channel.send_file("test", Path("test.txt"), "caption")
    out, err = capsys.readouterr()
    assert "Sending file" in out
    assert "test.txt" in out
    assert "caption" in out

    await channel.stop()
    out, err = capsys.readouterr()
    assert "CLI Channel stopped" in out
