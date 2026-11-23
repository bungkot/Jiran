import pytest

from agent.config import Settings
from agent.tools import Sandbox


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path / "ws")


@pytest.fixture
def settings(tmp_path):
    return Settings(
        workspace_dir=tmp_path / "ws",
        logs_dir=tmp_path / "logs",
        command_timeout_s=10,
        max_output_chars=500,
    )
