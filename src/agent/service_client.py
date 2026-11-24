"""Klien tipis untuk mengirim goal ke layanan standby dan menunggu hasilnya."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from .config import Settings

JsonPost = Callable[[str, str, float], dict[str, Any]]
JsonGet = Callable[[str, str, float], dict[str, Any]]
TERMINAL_STATUSES = frozenset({"completed", "failed"})


class ServiceError(RuntimeError):
    """Goal tidak dapat dikirim atau hasilnya tidak dapat diambil."""

    pass


def _headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _http_error(exc: httpx.HTTPError) -> str:
    """Ringkas error HTTP; sertakan pesan dari layanan (mis. 401/400) bila ada."""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            detail = exc.response.json().get("error", "")
        except (ValueError, AttributeError):
            detail = ""
        return f"HTTP {exc.response.status_code}" + (f": {detail}" if detail else "")
    return str(exc)


def post_goal(
    base_url: str,
    goal: str,
    timeout_s: float,
    *,
    options: dict[str, Any] | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"goal": goal, **(options or {})}
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(f"{base_url}/goals", json=body, headers=_headers(token))
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise ServiceError(f"Gagal mengirim goal ke {base_url}: {_http_error(exc)}") from exc
    except ValueError as exc:
        raise ServiceError(f"Gagal mengirim goal ke {base_url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ServiceError("Respons submit bukan objek JSON.")
    return payload


def get_job(
    base_url: str, job_id: str, timeout_s: float, *, token: str | None = None
) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.get(f"{base_url}/goals/{job_id}", headers=_headers(token))
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise ServiceError(f"Gagal mengambil status job {job_id}: {_http_error(exc)}") from exc
    except ValueError as exc:
        raise ServiceError(f"Gagal mengambil status job {job_id}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ServiceError("Respons status bukan objek JSON.")
    return payload


def submit_goal(
    goal: str,
    settings: Settings,
    *,
    wait: bool = True,
    options: dict[str, Any] | None = None,
    post: JsonPost | None = None,
    get: JsonGet | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Kirim satu goal; secara default tunggu sampai job selesai lalu kembalikan hasilnya."""
    target = goal.strip()
    if not target:
        raise ServiceError("Goal tidak boleh kosong.")
    base_url = settings.service_url
    submit = post or post_goal
    fetch = get or get_job
    # Argumen tambahan hanya dikirim bila dipakai, agar pengganti sederhana
    # (post/get dengan tiga argumen) tetap kompatibel.
    extra: dict[str, Any] = {}
    if options:
        extra["options"] = options
    if settings.service_token:
        extra["token"] = settings.service_token
    fetch_extra = {"token": settings.service_token} if settings.service_token else {}
    response = submit(
        base_url, target, min(30.0, float(settings.service_submit_timeout_s)), **extra
    )
    job_id = response.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise ServiceError("Layanan tidak mengembalikan id job.")
    if not wait:
        return response

    deadline = time.monotonic() + settings.service_submit_timeout_s
    last_progress: tuple[Any, Any, Any] | None = None
    consecutive_errors = 0
    while True:
        try:
            job = fetch(base_url, job_id, 30.0, **fetch_extra)
            consecutive_errors = 0
        except ServiceError as exc:
            # Job sudah diterima layanan; jangan kehilangan id hanya karena
            # gangguan jaringan sesaat saat polling.
            consecutive_errors += 1
            if consecutive_errors >= 3 or time.monotonic() >= deadline:
                raise ServiceError(
                    f"Job {job_id}: gagal mengambil status ({exc}). "
                    f"Cek dengan: coding-agent job {job_id}"
                ) from exc
            time.sleep(min(settings.service_poll_interval_s * consecutive_errors, 5.0))
            continue
        progress_key = (job.get("status"), job.get("progress_seq"), job.get("progress"))
        if on_progress is not None and progress_key != last_progress:
            on_progress(job)
            last_progress = progress_key
        if job.get("status") in TERMINAL_STATUSES:
            return job
        if time.monotonic() >= deadline:
            # Satu pengecekan terakhir sebelum menyerah.
            try:
                final = fetch(base_url, job_id, 30.0, **fetch_extra)
            except ServiceError:
                final = None
            if final is not None and final.get("status") in TERMINAL_STATUSES:
                return final
            raise ServiceError(
                f"Job {job_id} belum selesai setelah batas waktu. "
                f"Cek dengan: coding-agent job {job_id}"
            )
        time.sleep(settings.service_poll_interval_s)


def fetch_job(job_id: str, settings: Settings) -> dict[str, Any]:
    """Ambil status satu job tanpa menunggu."""
    return get_job(settings.service_url, job_id, 30.0, token=settings.service_token)
