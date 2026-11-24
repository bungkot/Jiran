"""Klien untuk menjalankan goal loop di dalam image Docker.

Image agent dipanggil lewat ``docker run`` dengan workspace dan log dipasang
pada path yang sama seperti di host, sehingga perintah verifikasi yang dijalankan
agent di container bersarang tetap melihat path yang valid dari sisi daemon.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import Settings
from .schemas import CommandResult
from .tools.container_runner import _force_remove_container, run_argv

Runner = Callable[..., CommandResult]

_SOCKET = "/var/run/docker.sock"


class ContainerError(RuntimeError):
    """Goal loop tidak dapat dijalankan sebagai container."""


@dataclass(frozen=True)
class ContainerGoalRequest:
    goal: str
    project: str | None = None
    verification_commands: tuple[str, ...] = ()
    required_files: tuple[str, ...] = ()
    max_iterations: int | None = None
    allow_no_changes: bool = False


@dataclass(frozen=True)
class ContainerGoalResult:
    container: str
    achieved: bool
    exit_code: int | None
    timed_out: bool
    output: str
    payload: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.achieved and self.exit_code == 0 and not self.timed_out


def container_ollama_url(url: str, network: str) -> str:
    """Ganti localhost dengan host-gateway hanya untuk jaringan bridge.

    Dengan ``--network host`` container memakai stack jaringan host, sehingga
    ``localhost`` sudah menunjuk ke Ollama di host.
    """
    if network == "host":
        return url
    parts = urlsplit(url)
    if parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return url
    netloc = "host.docker.internal"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _docker_socket_gid() -> int | None:
    try:
        return os.stat(_SOCKET).st_gid
    except OSError:
        return None


def _escape_mount_source(path: Path) -> str:
    return str(path).replace(",", r"\,")


def _settings_env(settings: Settings, workspace: Path, logs: Path) -> list[str]:
    values: list[tuple[str, str]] = [
        (
            "OLLAMA_BASE_URL",
            container_ollama_url(settings.ollama_base_url, settings.container_network),
        ),
        ("OLLAMA_MODEL", settings.ollama_model),
        ("OLLAMA_NUM_CTX", str(settings.ollama_num_ctx)),
        ("OLLAMA_KEEP_ALIVE", settings.ollama_keep_alive),
        ("LLM_TEMPERATURE", str(settings.llm_temperature)),
        ("LLM_TIMEOUT_S", str(settings.llm_timeout_s)),
        ("LLM_MAX_RETRIES", str(settings.llm_max_retries)),
        ("MAX_CONTEXT_CHARS", str(settings.max_context_chars)),
        ("MAX_AGENT_STEPS", str(settings.max_agent_steps)),
        ("MAX_AGENT_TOOL_CALLS", str(settings.max_agent_tool_calls)),
        ("MAX_FIX_ITER", str(settings.max_fix_iterations)),
        ("MAX_GOAL_ITER", str(settings.max_goal_iterations)),
        ("GOAL_STUCK_LIMIT", str(settings.goal_stuck_limit)),
        ("COMMAND_TIMEOUT_S", str(settings.command_timeout_s)),
        ("MAX_OUTPUT_CHARS", str(settings.max_output_chars)),
        ("MAX_FILE_BYTES", str(settings.max_file_bytes)),
        ("WORKSPACE_DIR", str(workspace)),
        ("AGENT_LOGS_DIR", str(logs)),
        ("AGENT_SANDBOX_NETWORK", settings.sandbox_network),
        ("AGENT_SANDBOX_MEMORY_MB", str(settings.sandbox_memory_mb)),
        ("AGENT_SANDBOX_CPUS", str(settings.sandbox_cpus)),
        ("AGENT_SANDBOX_PIDS_LIMIT", str(settings.sandbox_pids_limit)),
    ]
    if settings.sandbox_image:
        values.append(("AGENT_SANDBOX_IMAGE", settings.sandbox_image))
    args: list[str] = []
    for key, value in values:
        args.extend(["--env", f"{key}={value}"])
    return args


def build_goal_container_argv(
    request: ContainerGoalRequest,
    settings: Settings,
    *,
    docker: str,
    workspace: Path,
    logs: Path,
    container_name: str,
) -> list[str]:
    """Bangun argv ``docker run`` tanpa shell; murni untuk dites."""
    if not request.goal.strip():
        raise ContainerError("Tujuan tidak boleh kosong.")
    args = [
        docker,
        "run",
        "--rm",
        "--init",
        "--name",
        container_name,
        "--network",
        settings.container_network,
    ]
    if settings.container_network == "bridge":
        args.extend(["--add-host", "host.docker.internal:host-gateway"])
    args.extend(_settings_env(settings, workspace, logs))
    args.extend(
        [
            "--mount",
            f"type=bind,source={_escape_mount_source(workspace)},target={workspace}",
            "--mount",
            f"type=bind,source={_escape_mount_source(logs)},target={logs}",
        ]
    )
    if settings.container_docker_socket:
        args.extend(["--mount", f"type=bind,source={_SOCKET},target={_SOCKET}"])
    if os.name != "nt":
        args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        socket_gid = _docker_socket_gid() if settings.container_docker_socket else None
        if socket_gid is not None and socket_gid != os.getgid():
            args.extend(["--group-add", str(socket_gid)])

    goal_args = ["goal", "--json"]
    if request.project:
        goal_args.extend(["--project", request.project])
    for command in request.verification_commands:
        goal_args.extend(["--verify", command])
    for path in request.required_files:
        goal_args.extend(["--require-file", path])
    if request.max_iterations is not None:
        goal_args.extend(["--max-iterations", str(request.max_iterations)])
    if request.allow_no_changes:
        goal_args.append("--allow-no-change")
    goal_args.extend(["--", request.goal])
    args.extend([settings.container_image, *goal_args])
    return args


def _extract_payload(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "achieved" in value:
            return value
    return None


def run_container_goal(
    request: ContainerGoalRequest,
    settings: Settings,
    *,
    runner: Runner | None = None,
    timeout_s: int | None = None,
    on_output: Callable[[str], None] | None = None,
) -> ContainerGoalResult:
    docker = shutil.which("docker")
    if not docker:
        raise ContainerError("Docker CLI tidak ditemukan di host.")
    if settings.container_docker_socket and not Path(_SOCKET).exists():
        raise ContainerError(f"Sock Docker {_SOCKET} tidak ditemukan; aktifkan Docker daemon.")

    workspace = settings.workspace_dir.resolve()
    logs = settings.logs_dir.resolve()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ContainerError(f"Gagal menyiapkan folder workspace/log: {exc}") from exc

    name = f"coding-agent-goal-{uuid.uuid4().hex[:12]}"
    argv = build_goal_container_argv(
        request,
        settings,
        docker=docker,
        workspace=workspace,
        logs=logs,
        container_name=name,
    )
    limit = settings.container_goal_timeout_s if timeout_s is None else timeout_s
    if limit < 1:
        raise ContainerError("Batas waktu container harus lebih besar dari 0.")
    run_kwargs: dict[str, Any] = {
        "cwd": workspace,
        "timeout_s": limit,
        "max_output_chars": max(settings.max_output_chars, 20_000),
    }
    if on_output is not None:

        def forward_output(chunk: str) -> None:
            if _extract_payload(chunk) is None:
                on_output(chunk)

        run_kwargs["on_output"] = forward_output
    result = (runner or run_argv)(argv, **run_kwargs)
    output = result.output
    if result.timed_out:
        _force_remove_container(docker, name)
        output += f"\nContainer dihentikan setelah batas {limit}s."
    payload = _extract_payload(output)
    achieved = result.exit_code == 0 and bool(payload and payload.get("achieved"))
    return ContainerGoalResult(
        container=name,
        achieved=achieved,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        output=output,
        payload=payload,
    )
