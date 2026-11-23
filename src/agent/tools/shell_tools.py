"""LangChain command tool backed exclusively by the Docker runner."""

from __future__ import annotations

from collections.abc import Callable

from langchain_core.tools import BaseTool, tool

from ..config import Settings
from .container_runner import run_command_in_container
from .sandbox import Sandbox, SandboxError


def build_shell_tools(
    sandbox: Sandbox,
    settings: Settings,
    *,
    on_output: Callable[[str], None] | None = None,
) -> list[BaseTool]:
    @tool
    def run_command(cmd: str, cwd: str = ".", timeout_s: int = 120) -> str:
        """Run a project command inside a disposable Docker container.
        `cwd` is relative to the project root. Container network access is off by default."""
        try:
            output_callback = {"on_output": on_output} if on_output is not None else {}
            return run_command_in_container(
                cmd,
                sandbox,
                settings,
                cwd,
                timeout_s,
                **output_callback,
            ).format()
        except SandboxError as exc:
            return f"ERROR: {exc}"

    return [run_command]
