"""Tests for CLI commands."""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

from corpclaw_lite.cli import _build_parser, cmd_containers, cmd_prune, cmd_user_create


def test_cli_help(capsys):
    parser = _build_parser()
    with contextlib.suppress(SystemExit):
        parser.parse_args(["--help"])
    captured = capsys.readouterr()
    assert "CorpClaw Lite" in captured.out


def test_user_create():
    with (
        patch("corpclaw_lite.users.manager.UserManager.create_user") as mock_create,
        patch("builtins.print"),
    ):
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.name = "test"
        mock_user.department = "it"
        mock_create.return_value = mock_user

        cmd_user_create(123, "it", "test")
        mock_create.assert_called_once()


def test_containers_list():
    with (
        patch("corpclaw_lite.container.manager.docker"),
        patch(
            "corpclaw_lite.container.manager.ContainerManager.list_active", new_callable=AsyncMock
        ) as mock_list,
        patch("builtins.print"),
    ):
        mock_list.return_value = ["container1", "container2"]

        cmd_containers()
        mock_list.assert_called_once()


def test_prune():
    with (
        patch("corpclaw_lite.container.manager.docker"),
        patch(
            "corpclaw_lite.container.manager.ContainerManager.prune_idle", new_callable=AsyncMock
        ) as mock_prune,
        patch("builtins.print"),
    ):
        mock_prune.return_value = 2

        cmd_prune()
        mock_prune.assert_called_once()


def test_plugin_list():
    from corpclaw_lite.cli import cmd_plugin_list

    with (
        patch("corpclaw_lite.extensions.plugins.registry.PluginRegistry.list_all") as mock_list,
        patch("corpclaw_lite.extensions.plugins.registry.PluginRegistry.load_directory"),
        patch("builtins.print"),
    ):
        mock_list.return_value = []
        cmd_plugin_list()
        mock_list.assert_called_once()


def test_skill_list():
    from corpclaw_lite.cli import cmd_skill_list

    with (
        patch("corpclaw_lite.extensions.skills.registry.SkillRegistry.list_all") as mock_list,
        patch("corpclaw_lite.extensions.skills.registry.SkillRegistry.load_directory"),
        patch("builtins.print"),
    ):
        mock_list.return_value = []
        cmd_skill_list()
        mock_list.assert_called_once()


def test_generate():
    from corpclaw_lite.cli import cmd_generate

    with (
        patch("pathlib.Path.mkdir"),
        patch("pathlib.Path.write_text") as mock_write,
        patch("builtins.print"),
    ):
        cmd_generate("skill", "my_new_skill")
        mock_write.assert_called_once()
