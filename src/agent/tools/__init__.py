from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from langchain_core.tools import BaseTool

from ..config import Settings
from .edit_tools import build_edit_tools
from .fs_tools import build_fs_tools
from .sandbox import Sandbox, SandboxError
from .shell_tools import build_shell_tools

__all__ = ["Sandbox", "SandboxError", "build_all_tools"]


def build_all_tools(
    settings: Settings,
    project_root: Path | None = None,
    *,
    on_output: Callable[[str], None] | None = None,
) -> list[BaseTool]:
    """Build file and isolated command tools scoped to one project directory."""
    sandbox = Sandbox(project_root or settings.workspace_dir)
    return [
        *build_fs_tools(
            sandbox,
            settings.max_file_bytes,
            min_free_disk_mb=settings.sandbox_min_free_disk_mb,
        ),
        *build_edit_tools(
            sandbox,
            settings.max_file_bytes,
            min_free_disk_mb=settings.sandbox_min_free_disk_mb,
        ),
        *build_shell_tools(sandbox, settings, on_output=on_output),
    ]
