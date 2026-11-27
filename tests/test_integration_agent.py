import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent.agent import run_agent
from agent.config import Settings


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RUN_AGENT_E2E") != "1",
    reason="set RUN_AGENT_E2E=1 untuk uji Ollama + Docker end-to-end",
)
def test_generic_agent_edits_and_verifies_python_project():
    with TemporaryDirectory(prefix="coding-agent-e2e-") as temporary:
        base = Path(temporary)
        workspace = base / "workspace"
        project = workspace / "sample"
        (project / "src").mkdir(parents=True)
        (project / "pyproject.toml").write_text(
            "[project]\nname = 'sample'\nversion = '0.1.0'\n",
            encoding="utf-8",
        )
        (project / ".env").write_text("API_TOKEN=must-not-be-mounted\n", encoding="utf-8")
        target = project / "src" / "main.py"
        target.write_text("MESSAGE = 'before'\n", encoding="utf-8")
        settings = replace(
            Settings.load(),
            workspace_dir=workspace,
            logs_dir=base / "logs",
            llm_timeout_s=120,
            command_timeout_s=180,
            max_agent_steps=12,
            max_fix_iterations=1,
            sandbox_memory_mb=2048,
        )

        result = run_agent(
            "Change MESSAGE in src/main.py to 'after'. Inspect and edit the file.",
            project,
            settings,
            verification_commands=[
                "python -c \"from pathlib import Path; assert Path('.env').read_text() == ''\"",
                "python -m py_compile src/main.py",
            ],
        )

        assert result.ok and result.verified and result.files_written > 0
        assert "MESSAGE = 'after'" in target.read_text(encoding="utf-8")
        assert result.verifications[0].result.output == ""
