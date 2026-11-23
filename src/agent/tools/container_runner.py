"""Bounded command execution in disposable Docker containers."""

from __future__ import annotations

import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ..config import Settings
from ..schemas import CommandResult
from .sandbox import Sandbox, SandboxError, is_sensitive_relative_path

_STACK_IMAGES = (
    (("package.json",), "node:22-bookworm-slim"),
    (("pyproject.toml", "requirements.txt", "Pipfile"), "python:3.13-slim"),
    (("Cargo.toml",), "rust:1-bookworm"),
    (("go.mod",), "golang:1.25-bookworm"),
    (("pom.xml",), "maven:3.9-eclipse-temurin-21"),
    (("build.gradle", "build.gradle.kts", "gradlew"), "gradle:8-jdk21"),
    (
        ("*.csproj", "*.fsproj", "*.vbproj", "*.sln", "global.json"),
        "mcr.microsoft.com/dotnet/sdk:10.0",
    ),
    (("Gemfile",), "ruby:3.4-slim"),
    (("composer.json",), "composer:2"),
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    try:
        if sys.platform == "win32":
            taskkill = shutil.which("taskkill")
            if taskkill:
                subprocess.run(  # noqa: S603 - argv tetap, bukan input tidak tepercaya
                    [taskkill, "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError, subprocess.SubprocessError):
        try:
            proc.kill()
        except ProcessLookupError:
            return


def _tail(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", len(text) > 0
    if len(text) <= limit:
        return text, False
    dropped = len(text) - limit
    return f"...[{dropped} karakter awal dibuang]...\n{text[-limit:]}", True


def run_argv(
    argv: list[str],
    cwd: Path,
    timeout_s: float,
    max_output_chars: int = 8000,
    on_output: Callable[[str], None] | None = None,
) -> CommandResult:
    """Run the Docker CLI without a host shell, with bounded output and timeout."""
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True

    start = time.perf_counter()
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv dibangun internal, bukan input mentah
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
    except OSError as exc:
        return CommandResult(
            argv=argv,
            exit_code=None,
            output=f"gagal memulai proses: {exc}",
            duration_s=time.perf_counter() - start,
        )

    timed_out = False
    if on_output is None:
        try:
            output, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(proc)
            output, _ = proc.communicate()
        output = _ANSI_RE.sub("", output or "")
        output, truncated = _tail(output, max_output_chars)
    else:
        chunks: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    chunks.put(line)
            finally:
                chunks.put(None)

        reader = threading.Thread(target=read_output, name="command-output", daemon=True)
        reader.start()
        deadline = start + timeout_s
        tail = ""
        total_chars = 0
        received_eof = False
        try:
            while not received_eof:
                remaining = deadline - time.perf_counter()
                if remaining <= 0 and not timed_out:
                    timed_out = True
                    _kill_process_tree(proc)
                try:
                    chunk = chunks.get(timeout=max(0.01, min(0.1, remaining)))
                except queue.Empty:
                    if proc.poll() is not None and not reader.is_alive():
                        received_eof = True
                    continue
                if chunk is None:
                    received_eof = True
                    continue
                cleaned = _ANSI_RE.sub("", chunk)
                total_chars += len(cleaned)
                if max_output_chars > 0:
                    tail = (tail + cleaned)[-max_output_chars:]
                on_output(cleaned)
        except BaseException:
            _kill_process_tree(proc)
            proc.wait()
            reader.join()
            raise
        proc.wait()
        truncated = total_chars > max(0, max_output_chars)
        if truncated:
            dropped = max(0, total_chars - max_output_chars)
            output = f"...[{dropped} karakter awal dibuang]...\n{tail}"
        else:
            output = tail
        if max_output_chars <= 0:
            output = ""
    output = output.strip()
    return CommandResult(
        argv=[Path(argv[0]).name, *argv[1:]],
        exit_code=None if timed_out else proc.returncode,
        timed_out=timed_out,
        truncated=truncated,
        output=output.strip(),
        duration_s=time.perf_counter() - start,
    )


def detect_sandbox_image(project_root: Path, override: str | None = None) -> str:
    if override:
        return override
    for manifests, image in _STACK_IMAGES:
        if any(
            (project_root / manifest).is_file() or bool(list(project_root.glob(manifest)))
            for manifest in manifests
        ):
            return image
    raise SandboxError(
        "Stack proyek tidak terdeteksi. Atur AGENT_SANDBOX_IMAGE ke image Linux "
        "yang berisi toolchain proyek."
    )


def _secret_mount_args(project_root: Path) -> list[str]:
    # Bind-mount sumber /dev/null diselesaikan sisi daemon Docker; dengan
    # kontainer Linux (Docker Desktop/WSL2) path itu ada di sisi VM, jadi
    # tidak perlu menggunakan path /dev/null pada host. Tidak ada gate
    # kematian karena kontainer membutuhkan Linux; jika daemon tidak
    # mendukung mount tersebut, error muncul pada waktu `docker run`.
    mounts: list[str] = []
    ignored_dirs = {
        ".git",
        ".next",
        ".venv",
        "build",
        "dist",
        "node_modules",
        "target",
        "vendor",
        "venv",
    }

    def raise_walk_error(error: OSError) -> None:
        raise SandboxError(f"gagal memeriksa file proyek untuk secret: {error}") from error

    for current, dirs, files in os.walk(project_root, followlinks=False, onerror=raise_walk_error):
        parent = Path(current)
        sensitive_dirs = [
            name
            for name in dirs
            if is_sensitive_relative_path((parent / name).relative_to(project_root).as_posix())
            and not (parent / name).is_symlink()
        ]
        for name in sensitive_dirs:
            relative = (parent / name).relative_to(project_root).as_posix()
            mounts.extend(
                [
                    "--tmpfs",
                    f"/workspace/{relative}:rw,nosuid,size=1m",
                ]
            )
        dirs[:] = [name for name in dirs if name not in ignored_dirs and name not in sensitive_dirs]
        for name in files:
            path = parent / name
            if path.is_symlink() or not is_sensitive_relative_path(
                path.relative_to(project_root).as_posix()
            ):
                continue
            relative = path.relative_to(project_root).as_posix()
            mounts.extend(
                [
                    "--mount",
                    f"type=bind,source=/dev/null,target=/workspace/{relative},readonly",
                ]
            )
    return mounts


def _docker_command(
    cmd: str,
    sandbox: Sandbox,
    settings: Settings,
    cwd: str,
) -> tuple[list[str], str]:
    if not cmd.strip():
        raise SandboxError("perintah kosong")
    if len(cmd) > 4000:
        raise SandboxError("perintah melebihi batas 4000 karakter")

    docker = shutil.which("docker")
    if not docker:
        raise SandboxError(
            "Docker CLI tidak ditemukan. Pasang Docker dan pastikan daemon berjalan."
        )

    work_dir = sandbox.resolve(cwd)
    if not work_dir.is_dir():
        raise SandboxError(f"cwd bukan folder yang ada: {cwd!r}")

    image = detect_sandbox_image(sandbox.root, settings.sandbox_image)
    container_name = f"coding-agent-{uuid.uuid4().hex[:16]}"
    relative_cwd = work_dir.relative_to(sandbox.root).as_posix()
    container_cwd = "/workspace" if relative_cwd == "." else f"/workspace/{relative_cwd}"
    mount_source = str(sandbox.root).replace(",", r"\,")
    temp_options = "rw,nosuid,size=256m"
    if sys.platform != "win32":
        temp_options += f",uid={os.getuid()},gid={os.getgid()}"
    args = [
        docker,
        "run",
        "--rm",
        "--pull=missing",
        "--init",
        "--name",
        container_name,
        "--network",
        settings.sandbox_network,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit",
        str(settings.sandbox_pids_limit),
        "--memory",
        f"{settings.sandbox_memory_mb}m",
        "--memory-swap",
        f"{settings.sandbox_memory_mb}m",
        "--cpus",
        str(settings.sandbox_cpus),
        "--tmpfs",
        f"/tmp:{temp_options}",  # noqa: S108 - /tmp adalah tmpfs container yang dimaksud
        "--env",
        "HOME=/tmp",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "npm_config_cache=/tmp/npm-cache",
        "--env",
        "PIP_CACHE_DIR=/tmp/pip-cache",
        "--env",
        "CARGO_HOME=/tmp/cargo",
        "--env",
        "GOCACHE=/tmp/go-cache",
        "--env",
        "NEXT_TELEMETRY_DISABLED=1",
        "--env",
        "NO_COLOR=1",
        "--env",
        "FORCE_COLOR=0",
        "--env",
        "npm_config_update_notifier=false",
        "--mount",
        f"type=bind,source={mount_source},target=/workspace",
    ]
    args.extend(_secret_mount_args(sandbox.root))
    if sys.platform != "win32":
        args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    args.extend(
        [
            "--workdir",
            container_cwd,
            image,
            "sh",
            "-lc",
            cmd,
        ]
    )
    return args, container_name


def _force_remove_container(docker: str, container_name: str) -> bool:
    """Best-effort hapus container yang tidak bisa di-stop."""
    try:
        removed = subprocess.run(  # noqa: S603 - argv tetap, bukan input tidak tepercaya
            [docker, "rm", "-f", container_name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return removed.returncode == 0


def run_command_in_container(
    cmd: str,
    sandbox: Sandbox,
    settings: Settings,
    cwd: str = ".",
    timeout_s: int | None = None,
    on_output: Callable[[str], None] | None = None,
) -> CommandResult:
    if timeout_s is not None and timeout_s < 1:
        raise SandboxError("timeout_s harus >= 1")
    argv, container_name = _docker_command(cmd, sandbox, settings, cwd)
    limit = max(1, min(timeout_s or settings.command_timeout_s, settings.command_timeout_s))
    result = run_argv(
        argv,
        cwd=sandbox.root,
        timeout_s=limit,
        max_output_chars=settings.max_output_chars,
        on_output=on_output,
    )
    if result.timed_out:
        docker = shutil.which("docker")
        if docker:
            try:
                stopped = subprocess.run(  # noqa: S603 - argv tetap, bukan input tidak tepercaya
                    [docker, "stop", "--time", "2", container_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if stopped.returncode:
                    detail = stopped.stderr.strip() or "exit code non-zero"
                    result.output += f"\nDocker stop gagal: {detail}"
                    if _force_remove_container(docker, container_name):
                        result.output += "\nContainer dihapus paksa."
                    else:
                        result.output += (
                            f"\nContainer mungkin masih berjalan; hapus manual: "
                            f"docker rm -f {container_name}"
                        )
            except OSError as exc:
                result.output += f"\nGagal menghentikan container setelah timeout: {exc}"
                _force_remove_container(docker, container_name)
            except subprocess.TimeoutExpired:
                result.output += "\nDocker stop melewati batas 10 detik."
                _force_remove_container(docker, container_name)
    return result
