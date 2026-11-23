"""FASE 0 — Preflight: pastikan lingkungan siap sebelum agent berjalan.

Gate lulus: tidak ada cek berstatus FAIL (WARN diperbolehkan).
Fungsi ini TIDAK memakai LLM, murni verifikasi lingkungan.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile

import httpx

from .config import Settings
from .schemas import CheckStatus, PreflightCheck, PreflightReport


def _normalize_model(name: str) -> str:
    """Ollama menyimpan 'nama' sebagai 'nama:latest' jika tanpa tag."""
    return name if ":" in name else f"{name}:latest"


def check_python(min_version: tuple[int, int] = (3, 11)) -> PreflightCheck:
    cur = sys.version_info[:3]
    text = ".".join(map(str, cur))
    if cur[:2] >= min_version:
        return PreflightCheck(name="python", status=CheckStatus.OK, detail=text)
    return PreflightCheck(
        name="python",
        status=CheckStatus.FAIL,
        detail=text,
        hint=f"Butuh Python >= {min_version[0]}.{min_version[1]}",
    )


def check_workspace(settings: Settings) -> PreflightCheck:
    """Pastikan folder workspace ada dan bisa ditulis."""
    try:
        settings.workspace_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=settings.workspace_dir, delete=True):
            pass
    except OSError as e:
        return PreflightCheck(
            name="workspace",
            status=CheckStatus.FAIL,
            detail=str(e),
            hint="Periksa izin tulis folder workspace",
        )
    return PreflightCheck(
        name="workspace", status=CheckStatus.OK, detail=str(settings.workspace_dir)
    )


def check_ollama(base_url: str, timeout: float = 5.0) -> tuple[PreflightCheck, list[str]]:
    """Cek server Ollama hidup. Return (hasil_cek, daftar_model_terpasang)."""
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return (
            PreflightCheck(
                name="ollama",
                status=CheckStatus.FAIL,
                detail=f"tidak terhubung ke {base_url}: {e}",
                hint="Jalankan `ollama serve` (atau buka aplikasi Ollama)",
            ),
            [],
        )
    if not isinstance(data, dict):
        return (
            PreflightCheck(
                name="ollama",
                status=CheckStatus.FAIL,
                detail=f"respons /api/tags tidak valid: {type(data).__name__}",
                hint="Pastikan alamat OLLAMA_BASE_URL mengarah ke server Ollama.",
            ),
            [],
        )
    raw_models = data.get("models", [])
    if not isinstance(raw_models, list):
        return (
            PreflightCheck(
                name="ollama",
                status=CheckStatus.FAIL,
                detail="respons /api/tags tidak valid: 'models' bukan list",
                hint="Pastikan alamat OLLAMA_BASE_URL mengarah ke server Ollama.",
            ),
            [],
        )
    models: list[str] = []
    for item in raw_models:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return (
                PreflightCheck(
                    name="ollama",
                    status=CheckStatus.FAIL,
                    detail="respons /api/tags tidak valid: entri model tidak dikenali",
                    hint="Pastikan alamat OLLAMA_BASE_URL mengarah ke server Ollama.",
                ),
                [],
            )
        models.append(item["name"])
    return (
        PreflightCheck(
            name="ollama", status=CheckStatus.OK, detail=f"{base_url} ({len(models)} model)"
        ),
        models,
    )


def check_model(model: str, available: list[str]) -> PreflightCheck:
    installed = {_normalize_model(name) for name in available if isinstance(name, str)}
    if _normalize_model(model) in installed:
        return PreflightCheck(name=f"model:{model}", status=CheckStatus.OK, detail="terpasang")
    return PreflightCheck(
        name=f"model:{model}",
        status=CheckStatus.FAIL,
        detail=f"tidak ada. Terpasang: {', '.join(available) or '-'}",
        hint=f"Jalankan `ollama pull {model}`",
    )


def check_model_tools(base_url: str, model: str, timeout: float = 10.0) -> PreflightCheck:
    """Cek apakah model mendukung tool calling (WARN, bukan FAIL)."""
    name = f"model-tools:{model}"
    try:
        resp = httpx.post(f"{base_url}/api/show", json={"model": model}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return PreflightCheck(
            name=name, status=CheckStatus.WARN, detail=f"tidak bisa membaca info model: {e}"
        )
    if not isinstance(data, dict):
        return PreflightCheck(
            name=name,
            status=CheckStatus.WARN,
            detail=f"respons /api/show tidak valid: {type(data).__name__}",
            hint="Perbarui Ollama, atau uji tool calling manual di Fase 2/5",
        )
    caps = data.get("capabilities")
    if caps is None:
        return PreflightCheck(
            name=name,
            status=CheckStatus.WARN,
            detail="versi Ollama tidak melaporkan 'capabilities'",
            hint="Perbarui Ollama, atau uji tool calling manual di Fase 2/5",
        )
    if not isinstance(caps, list):
        return PreflightCheck(
            name=name,
            status=CheckStatus.WARN,
            detail=f"capabilities tidak valid: {caps!r}",
            hint="Perbarui Ollama, atau uji tool calling manual di Fase 2/5",
        )
    if "tools" in caps:
        return PreflightCheck(name=name, status=CheckStatus.OK, detail="mendukung tools")
    return PreflightCheck(
        name=name,
        status=CheckStatus.WARN,
        detail=f"capabilities: {caps}",
        hint="Model tanpa tool calling: gunakan model lain (mis. qwen3 / llama3.1) untuk Fase 5+",
    )


def check_docker() -> PreflightCheck:
    """Verify that the Docker CLI and daemon are usable."""
    path = shutil.which("docker")
    if not path:
        return PreflightCheck(
            name="docker",
            status=CheckStatus.FAIL,
            detail="'docker' tidak ditemukan di PATH",
            hint="Pasang Docker Engine/Desktop dan jalankan daemon.",
        )
    try:
        result = subprocess.run(  # noqa: S603 - argv tetap, bukan input tidak tepercaya
            [path, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PreflightCheck(
            name="docker",
            status=CheckStatus.FAIL,
            detail=f"gagal menghubungi daemon Docker: {exc}",
            hint="Pastikan daemon Docker aktif dan dapat diakses oleh pengguna ini.",
        )
    version = result.stdout.strip()
    if result.returncode != 0 or not version:
        detail = result.stderr.strip() or "Docker daemon tidak merespons."
        return PreflightCheck(
            name="docker",
            status=CheckStatus.FAIL,
            detail=detail,
            hint="Pastikan daemon Docker aktif dan dapat diakses oleh pengguna ini.",
        )
    return PreflightCheck(name="docker", status=CheckStatus.OK, detail=version)


def run_agent_preflight(settings: Settings) -> PreflightReport:
    """Preflight for stack-neutral runs; host Node/npm are not required."""
    report = PreflightReport()
    report.checks.extend(
        [
            check_python(),
            check_workspace(settings),
            check_docker(),
        ]
    )
    ollama_check, models = check_ollama(settings.ollama_base_url)
    report.checks.append(ollama_check)
    if not ollama_check.ok:
        report.checks.append(
            PreflightCheck(
                name=f"model:{settings.ollama_model}",
                status=CheckStatus.FAIL,
                detail="dilewati karena Ollama tidak terhubung",
            )
        )
        return report

    model_check = check_model(settings.ollama_model, models)
    report.checks.append(model_check)
    if model_check.ok:
        tool_check = check_model_tools(settings.ollama_base_url, settings.ollama_model)
        if tool_check.status != CheckStatus.OK:
            tool_check.status = CheckStatus.FAIL
        report.checks.append(tool_check)
    return report
