"""Konfigurasi terpusat. Nilai dibaca dari environment / file .env."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"})
_MIN_TOKEN_CHARS = 32


class SettingsError(ValueError):
    """Nilai environment tidak valid atau tidak konsisten."""


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} harus bilangan bulat, diterima {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} harus angka, diterima {raw!r}") from exc
    if not math.isfinite(value):
        raise SettingsError(f"{name} harus angka hingga, diterima {raw!r}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} harus bernilai true/false, diterima {raw!r}")


def _env_path(name: str, default: str) -> Path:
    raw = os.environ.get(name)
    if raw is not None and not raw.strip():
        raise SettingsError(f"{name} tidak boleh kosong")
    return Path(_get(name, default))


@dataclass(frozen=True)
class Settings:
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.5:4b"
    llm_temperature: float = 0.0
    llm_timeout_s: float = 180.0
    llm_max_retries: int = 2
    ollama_num_ctx: int = 8192
    ollama_keep_alive: str = "30m"
    max_context_chars: int = 12_000
    workspace_dir: Path = PROJECT_ROOT / "workspace"
    command_timeout_s: int = 300
    max_output_chars: int = 8000
    max_file_bytes: int = 200_000
    max_fix_iterations: int = 3
    max_agent_steps: int = 15
    max_agent_tool_calls: int = 30
    max_goal_iterations: int = 5
    max_goal_iterations_limit: int = 50
    require_judge_on_fast_path: bool = False
    goal_stuck_limit: int = 2
    container_image: str = "coding-agent:local"
    container_goal_timeout_s: int = 900
    container_docker_socket: bool = False
    container_network: str = "host"
    service_host: str = "127.0.0.1"
    service_port: int = 8765
    service_url: str = "http://localhost:8765"
    service_instance_id: str = "agent"
    service_submit_timeout_s: int = 1800
    service_poll_interval_s: float = 1.0
    service_token: str | None = None
    sandbox_image: str | None = None
    sandbox_network: str = "none"
    sandbox_memory_mb: int = 4096
    sandbox_cpus: float = 2.0
    sandbox_pids_limit: int = 128
    sandbox_min_free_disk_mb: int = 256
    sandbox_disk_quota_mb: int = 0  # 0 = nonaktif; --storage-opt pada container
    logs_dir: Path = PROJECT_ROOT / "logs"

    def __post_init__(self) -> None:
        if not self.ollama_base_url.strip():
            raise SettingsError("OLLAMA_BASE_URL tidak boleh kosong")
        if not str(self.workspace_dir).strip() or not str(self.logs_dir).strip():
            raise SettingsError("WORKSPACE_DIR dan AGENT_LOGS_DIR tidak boleh kosong")
        if self.sandbox_network not in {"none", "bridge"}:
            raise SettingsError("AGENT_SANDBOX_NETWORK harus 'none' atau 'bridge'")
        if self.sandbox_image and (
            any(c.isspace() for c in self.sandbox_image) or self.sandbox_image.startswith("-")
        ):
            raise SettingsError("AGENT_SANDBOX_IMAGE tidak valid")
        if self.command_timeout_s < 1:
            raise SettingsError("COMMAND_TIMEOUT_S harus lebih besar dari 0")
        if self.max_agent_steps < 1 or self.max_agent_tool_calls < 1:
            raise SettingsError("Batas langkah dan tool agent harus lebih besar dari 0")
        if self.max_goal_iterations < 1:
            raise SettingsError("MAX_GOAL_ITER harus lebih besar dari 0")
        if self.max_goal_iterations_limit < 1:
            raise SettingsError("MAX_GOAL_ITER_LIMIT harus lebih besar dari 0")
        if self.max_goal_iterations > self.max_goal_iterations_limit:
            raise SettingsError(
                "MAX_GOAL_ITER tidak boleh melebihi MAX_GOAL_ITER_LIMIT "
                f"({self.max_goal_iterations_limit})"
            )
        if self.goal_stuck_limit < 1:
            raise SettingsError("GOAL_STUCK_LIMIT harus lebih besar dari 0")
        if (
            not self.container_image.strip()
            or any(c.isspace() for c in self.container_image)
            or self.container_image.startswith("-")
        ):
            raise SettingsError("AGENT_CONTAINER_IMAGE tidak valid")
        if self.container_goal_timeout_s < 1:
            raise SettingsError("CONTAINER_GOAL_TIMEOUT_S harus lebih besar dari 0")
        if self.container_network not in {"bridge", "host"}:
            raise SettingsError("AGENT_CONTAINER_NETWORK harus 'bridge' atau 'host'")
        if not self.service_host.strip() or not self.service_url.strip():
            raise SettingsError("AGENT_SERVICE_HOST dan AGENT_SERVICE_URL tidak boleh kosong")
        if not re.fullmatch(r"[a-z0-9-]+", self.service_instance_id):
            raise SettingsError("AGENT_INSTANCE_ID hanya boleh berisi huruf kecil, angka, dan '-'")
        parsed_url = urlsplit(self.service_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise SettingsError("AGENT_SERVICE_URL harus berupa URL http/https yang valid")
        if not 1 <= self.service_port <= 65535:
            raise SettingsError("AGENT_SERVICE_PORT harus antara 1 dan 65535")
        # Endpoint /goals memicu agent menulis file dan menjalankan perintah.
        # Bind non-loopback tanpa token tidak boleh dimulai (fail-closed).
        if self.service_host.strip().lower() not in _LOOPBACK_HOSTS and not self.service_token:
            raise SettingsError(
                "AGENT_API_TOKEN wajib diisi bila AGENT_SERVICE_HOST bukan loopback"
            )
        if self.service_token and len(self.service_token) < _MIN_TOKEN_CHARS:
            raise SettingsError(f"AGENT_API_TOKEN harus minimal {_MIN_TOKEN_CHARS} karakter")
        if self.service_submit_timeout_s < 1:
            raise SettingsError("SERVICE_SUBMIT_TIMEOUT_S harus lebih besar dari 0")
        if self.service_poll_interval_s <= 0 or self.service_poll_interval_s > 60:
            raise SettingsError("SERVICE_POLL_INTERVAL_S harus antara 0 dan 60 detik")
        if self.max_fix_iterations < 0:
            raise SettingsError("MAX_FIX_ITER tidak boleh negatif")
        if self.sandbox_memory_mb < 256 or self.sandbox_cpus <= 0 or self.sandbox_pids_limit < 1:
            raise SettingsError("Batas resource sandbox tidak valid")
        if self.sandbox_min_free_disk_mb < 0:
            raise SettingsError("AGENT_SANDBOX_MIN_FREE_DISK_MB tidak boleh negatif")
        if self.sandbox_disk_quota_mb < 0:
            raise SettingsError("AGENT_SANDBOX_DISK_QUOTA_MB tidak boleh negatif")
        if not 0 <= self.llm_temperature <= 2:
            raise SettingsError("LLM_TEMPERATURE harus di antara 0 dan 2")
        if self.llm_timeout_s <= 0:
            raise SettingsError("LLM_TIMEOUT_S harus lebih besar dari 0")
        if self.llm_max_retries < 0:
            raise SettingsError("LLM_MAX_RETRIES tidak boleh negatif")
        if self.max_context_chars < 1000:
            raise SettingsError("MAX_CONTEXT_CHARS harus >= 1000")
        if self.ollama_num_ctx < 2048:
            raise SettingsError("OLLAMA_NUM_CTX harus >= 2048")
        if not self.ollama_keep_alive.strip():
            raise SettingsError("OLLAMA_KEEP_ALIVE tidak boleh kosong")
        if self.max_output_chars < 1:
            raise SettingsError("MAX_OUTPUT_CHARS harus >= 1")
        if self.max_file_bytes < 1:
            raise SettingsError("MAX_FILE_BYTES harus >= 1")

    @classmethod
    def load(cls) -> Settings:
        load_dotenv(PROJECT_ROOT / ".env")
        return cls(
            ollama_base_url=_get("OLLAMA_BASE_URL", cls.ollama_base_url).rstrip("/"),
            ollama_model=_get("OLLAMA_MODEL", cls.ollama_model),
            llm_temperature=_env_float("LLM_TEMPERATURE", cls.llm_temperature),
            llm_timeout_s=_env_float("LLM_TIMEOUT_S", cls.llm_timeout_s),
            llm_max_retries=_env_int("LLM_MAX_RETRIES", cls.llm_max_retries),
            ollama_num_ctx=_env_int("OLLAMA_NUM_CTX", cls.ollama_num_ctx),
            ollama_keep_alive=_get("OLLAMA_KEEP_ALIVE", cls.ollama_keep_alive),
            max_context_chars=_env_int("MAX_CONTEXT_CHARS", cls.max_context_chars),
            workspace_dir=_env_path("WORKSPACE_DIR", str(cls.workspace_dir)),
            command_timeout_s=_env_int("COMMAND_TIMEOUT_S", cls.command_timeout_s),
            max_output_chars=_env_int("MAX_OUTPUT_CHARS", cls.max_output_chars),
            max_file_bytes=_env_int("MAX_FILE_BYTES", cls.max_file_bytes),
            max_fix_iterations=_env_int("MAX_FIX_ITER", cls.max_fix_iterations),
            max_agent_steps=_env_int("MAX_AGENT_STEPS", cls.max_agent_steps),
            max_agent_tool_calls=_env_int("MAX_AGENT_TOOL_CALLS", cls.max_agent_tool_calls),
            max_goal_iterations=_env_int("MAX_GOAL_ITER", cls.max_goal_iterations),
            max_goal_iterations_limit=_env_int(
                "MAX_GOAL_ITER_LIMIT", cls.max_goal_iterations_limit
            ),
            require_judge_on_fast_path=_env_bool(
                "REQUIRE_JUDGE_ON_FAST_PATH", cls.require_judge_on_fast_path
            ),
            goal_stuck_limit=_env_int("GOAL_STUCK_LIMIT", cls.goal_stuck_limit),
            container_image=_get("AGENT_CONTAINER_IMAGE", cls.container_image),
            container_goal_timeout_s=_env_int(
                "CONTAINER_GOAL_TIMEOUT_S", cls.container_goal_timeout_s
            ),
            container_docker_socket=_env_bool(
                "AGENT_CONTAINER_DOCKER_SOCKET", cls.container_docker_socket
            ),
            container_network=_get("AGENT_CONTAINER_NETWORK", cls.container_network),
            service_host=_get("AGENT_SERVICE_HOST", cls.service_host),
            service_port=_env_int("AGENT_SERVICE_PORT", cls.service_port),
            service_url=_get("AGENT_SERVICE_URL", cls.service_url).rstrip("/"),
            service_instance_id=_get("AGENT_INSTANCE_ID", cls.service_instance_id),
            service_submit_timeout_s=_env_int(
                "SERVICE_SUBMIT_TIMEOUT_S", cls.service_submit_timeout_s
            ),
            service_poll_interval_s=_env_float(
                "SERVICE_POLL_INTERVAL_S", cls.service_poll_interval_s
            ),
            service_token=os.environ.get("AGENT_API_TOKEN") or None,
            sandbox_image=os.environ.get("AGENT_SANDBOX_IMAGE") or None,
            sandbox_network=_get("AGENT_SANDBOX_NETWORK", cls.sandbox_network),
            sandbox_memory_mb=_env_int("AGENT_SANDBOX_MEMORY_MB", cls.sandbox_memory_mb),
            sandbox_cpus=_env_float("AGENT_SANDBOX_CPUS", cls.sandbox_cpus),
            sandbox_pids_limit=_env_int("AGENT_SANDBOX_PIDS_LIMIT", cls.sandbox_pids_limit),
            sandbox_min_free_disk_mb=_env_int(
                "AGENT_SANDBOX_MIN_FREE_DISK_MB", cls.sandbox_min_free_disk_mb
            ),
            sandbox_disk_quota_mb=_env_int(
                "AGENT_SANDBOX_DISK_QUOTA_MB", cls.sandbox_disk_quota_mb
            ),
            logs_dir=_env_path("AGENT_LOGS_DIR", str(cls.logs_dir)),
        )
