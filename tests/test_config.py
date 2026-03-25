import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from corpclaw_lite.config.loader import load_settings


def test_load_settings_missing_file() -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = load_settings("non_existent_file.yaml")
        assert settings.llm.default == "local"
        assert settings.agent.max_steps == 15


def test_load_settings_expansion() -> None:
    config_yaml = """
llm:
  default: "cloud"
  named:
    cloud:
      type: anthropic
      model: ${TEST_MODEL:-claude-3}
      api_key: ${TEST_API_KEY}
agent:
  max_steps: 15
"""
    env_vars = {"TEST_API_KEY": "sk-ant-test"}
    with patch.dict(os.environ, env_vars, clear=True):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(config_yaml)
            temp_path = f.name

        try:
            settings = load_settings(Path(temp_path))

            assert settings.llm.default == "cloud"
            cloud_provider = settings.llm.named["cloud"]
            assert cloud_provider.type == "anthropic"
            assert cloud_provider.model == "claude-3"
            assert cloud_provider.api_key == "sk-ant-test"

        finally:
            os.remove(temp_path)
