"""Layanan goal standby: menerima banyak permintaan, memproses satu per satu.

HTTP server melayani submit/status secara konkuren, tetapi hanya ada satu worker
yang menjalankan ``run_goal_loop``. Dengan demikian beberapa klien boleh mengirim
goal bersamaan tanpa dua proyek dikerjakan dalam waktu yang sama.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import queue
import signal
import socket
import threading
import time
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from .agent import AgentCancelled
from .config import Settings
from .goal import (
    GoalAcceptance,
    GoalError,
    project_slug,
    run_goal_loop,
    visible_project_files,
)
from .progress import summarize_event

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


MAX_BODY_BYTES = 32_768
_MAX_VERIFY = 10
_MAX_REQUIRED_FILES = 50
_MAX_COMMAND_CHARS = 4000
_MAX_QUEUED_JOBS = 100
_DEFAULT_ITERATION_LIMIT = 50
_MAX_STREAM_EVENTS_PER_JOB = 512
_MAX_ARCHIVE_BYTES = 50 * 1024 * 1024


class QueueFullError(RuntimeError):
    """Antrean penuh; klien harus mencoba lagi nanti."""


def parse_goal_options(
    payload: dict[str, Any], *, iteration_limit: int = _DEFAULT_ITERATION_LIMIT
) -> dict[str, Any]:
    """Validasi opsi goal opsional dari body POST; lempar ValueError bila tidak valid."""
    options: dict[str, Any] = {}

    project = payload.get("project")
    if project is not None:
        if not isinstance(project, str) or not project.strip():
            raise ValueError("field 'project' harus berupa teks tidak kosong")
        options["project"] = project.strip()

    for key, limit in (("verify", _MAX_VERIFY), ("require_files", _MAX_REQUIRED_FILES)):
        value = payload.get(key)
        if value is None:
            continue
        if (
            not isinstance(value, list)
            or len(value) > limit
            or not all(isinstance(item, str) and item.strip() for item in value)
            or any(len(item) > _MAX_COMMAND_CHARS for item in value)
        ):
            raise ValueError(f"field '{key}' harus berupa daftar teks (maks {limit} item)")
        options[key] = [item.strip() for item in value]

    iterations = payload.get("max_iterations")
    if iterations is not None:
        if (
            isinstance(iterations, bool)
            or not isinstance(iterations, int)
            or not 1 <= iterations <= iteration_limit
        ):
            raise ValueError(f"field 'max_iterations' harus bilangan bulat 1..{iteration_limit}")
        options["max_iterations"] = iterations

    allow = payload.get("allow_no_changes")
    if allow is not None:
        if not isinstance(allow, bool):
            raise ValueError("field 'allow_no_changes' harus boolean")
        options["allow_no_changes"] = allow
    return options


def goal_kwargs(options: dict[str, Any]) -> dict[str, Any]:
    """Petakan opsi job ke argumen keyword ``run_goal_loop``."""
    kwargs: dict[str, Any] = {}
    if "project" in options:
        kwargs["project_name"] = options["project"]
    if options.get("verify") or options.get("require_files"):
        kwargs["acceptance"] = GoalAcceptance(
            verification_commands=tuple(options.get("verify", ())),
            required_files=tuple(options.get("require_files", ())),
        )
    if "max_iterations" in options:
        kwargs["max_iterations"] = options["max_iterations"]
    if options.get("allow_no_changes"):
        kwargs["allow_no_changes"] = True
    return kwargs


@dataclass
class Job:
    id: str
    goal: str
    container_id: str = ""
    status: str = "queued"
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    achieved: bool = False
    reason: str = ""
    project: str = ""
    iterations: int = 0
    files_written: int = 0
    files: list[str] = field(default_factory=list)
    response: str = ""
    error: str = ""  # detail internal; hanya ditulis ke log/disk
    error_public: str = ""  # pesan aman untuk klien HTTP
    progress: str = ""
    progress_event: str = ""
    progress_seq: int = 0
    events: list[dict[str, Any]] = field(default_factory=list, repr=False)
    options: dict[str, Any] = field(default_factory=dict, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "container_id": self.container_id,
            "goal": self.goal,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "achieved": self.achieved,
            "reason": self.reason,
            "project": self.project,
            "iterations": self.iterations,
            "files_written": self.files_written,
            "files": self.files,
            "response": self.response,
            "error": self.error_public,
            "progress": self.progress,
            "progress_event": self.progress_event,
            "progress_seq": self.progress_seq,
        }

    def metadata(self) -> dict[str, Any]:
        data = self.to_dict()
        data.pop("goal", None)
        data.pop("response", None)
        # Audit trail lokal menyimpan detail error lengkap; API tidak.
        data["error"] = self.error
        return data


class AgentService:
    def __init__(self, settings: Settings, run_goal: Any | None = None) -> None:
        self.settings = settings
        self._run_goal = run_goal or run_goal_loop
        self.container_id = socket.gethostname()
        self._queue: queue.Queue[Job] = queue.Queue(maxsize=_MAX_QUEUED_JOBS)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._metrics_registry = CollectorRegistry()
        self._jobs_submitted = Counter(
            "jiran_agent_jobs_submitted_total",
            "Total jobs accepted by this agent.",
            registry=self._metrics_registry,
        )
        self._jobs_finished = Counter(
            "jiran_agent_jobs_finished_total",
            "Total jobs finished by this agent.",
            labelnames=("status",),
            registry=self._metrics_registry,
        )
        self._queue_size = Gauge(
            "jiran_agent_queue_size",
            "Number of jobs waiting in this agent's queue.",
            registry=self._metrics_registry,
        )
        self._running_jobs = Gauge(
            "jiran_agent_running_jobs",
            "Number of jobs currently running on this agent.",
            registry=self._metrics_registry,
        )
        self._job_duration = Histogram(
            "jiran_agent_job_duration_seconds",
            "Time spent processing a job, excluding time in the queue.",
            labelnames=("status",),
            registry=self._metrics_registry,
        )
        self._jobs_dir = settings.logs_dir.resolve() / "jobs"
        self._locks_dir = settings.logs_dir.resolve() / "project-locks"
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._work, name="goal-worker", daemon=True)

    def start(self) -> None:
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        self._locks_dir.mkdir(parents=True, exist_ok=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker.is_alive():
            self._worker.join(timeout=5.0)

    def submit(self, goal: str, options: dict[str, Any] | None = None) -> Job:
        target = goal.strip()
        if not target:
            raise ValueError("Goal tidak boleh kosong.")
        with self._changed:
            running = sum(item.status in {"running", "cancelling"} for item in self._jobs.values())
            if self._queue.qsize() + running >= _MAX_QUEUED_JOBS:
                raise QueueFullError("Antrean goal penuh; coba lagi nanti.")
            job = Job(
                id=f"{self.settings.service_instance_id}_{uuid.uuid4().hex}",
                goal=target,
                container_id=self.container_id,
                created_at=_now(),
                options=dict(options or {}),
            )
            self._jobs[job.id] = job
            self._record_event_locked(job, "progress", "Job masuk antrean")
            self._queue.put(job)
            self._jobs_submitted.inc()
        self._persist(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return (
                Job(
                    **{
                        **job.__dict__,
                        "events": list(job.events),
                        "files": list(job.files),
                    }
                )
                if job
                else None
            )

    def cancel(self, job_id: str) -> Job | None:
        with self._changed:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status in TERMINAL_STATUSES:
                return Job(**{**job.__dict__, "events": list(job.events), "files": list(job.files)})
            job.cancel_event.set()
            job.status = "cancelling"
            job.progress = "Permintaan berhenti diterima"
            job.progress_event = "cancel.requested"
            self._record_event_locked(job, "progress", job.progress)
            snapshot = Job(**{**job.__dict__, "events": list(job.events), "files": list(job.files)})
        self._persist(job)
        return snapshot

    def list_jobs(self) -> list[Job]:
        with self._lock:
            return [
                Job(
                    **{
                        **job.__dict__,
                        "events": list(job.events),
                        "files": list(job.files),
                    }
                )
                for job in self._jobs.values()
            ]

    def wait_for_events(
        self, job_id: str, after: int, timeout: float
    ) -> tuple[list[dict[str, Any]], str, bool] | None:
        with self._changed:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if not any(event["id"] > after for event in job.events) and (
                job.status not in TERMINAL_STATUSES or job.finished_at is None
            ):
                self._changed.wait(timeout)
                job = self._jobs.get(job_id)
                if job is None:
                    return None
            events = [dict(event) for event in job.events if event["id"] > after]
            return events, job.status, job.finished_at is not None

    def _record_event_locked(self, job: Job, event: str, text: str) -> None:
        job.progress_seq += 1
        job.events.append(
            {
                "id": job.progress_seq,
                "event": event,
                "text": text,
                "created_at": _now(),
            }
        )
        if len(job.events) > _MAX_STREAM_EVENTS_PER_JOB:
            del job.events[:-_MAX_STREAM_EVENTS_PER_JOB]
        self._changed.notify_all()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            running = sum(job.status in {"running", "cancelling"} for job in self._jobs.values())
        return {"queued": self._queue.qsize(), "running": running}

    def prometheus_metrics(self) -> bytes:
        stats = self.stats()
        self._queue_size.set(stats["queued"])
        self._running_jobs.set(stats["running"])
        return generate_latest(self._metrics_registry)

    def _persist(self, job: Job) -> None:
        """Tulis metadata secara atomik.

        `write_text()` memotong file sebelum menulis; klien/tes yang membaca
        bersamaan bisa mendapat JSON kosong. Tulis ke file sementara lalu
        `replace()` agar pembaca selalu melihat versi lama atau versi lengkap.
        """
        try:
            self._jobs_dir.mkdir(parents=True, exist_ok=True)
            path = self._jobs_dir / f"job-{job.id}.json"
            temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(
                json.dumps(job.metadata(), ensure_ascii=True) + "\n", encoding="utf-8"
            )
            temporary.replace(path)
        except OSError:
            pass

    def _publish_progress(self, job: Job, event: dict[str, Any]) -> None:
        message = summarize_event(event)
        if message is None:
            return
        with self._changed:
            if event.get("event") == "goal.started":
                project = event.get("project")
                if isinstance(project, str):
                    job.project = project
            job.progress = message
            job.progress_event = str(event.get("event", ""))
            self._record_event_locked(job, "progress", message)
        self._persist(job)

    @contextmanager
    def _project_lock(self, job: Job) -> Iterator[None]:
        project = job.options.get("project")
        key = project if isinstance(project, str) else project_slug(job.goal)
        lock_path = self._locks_dir / f"{hashlib.sha256(key.encode()).hexdigest()}.lock"
        lock_file = lock_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            started = time.monotonic()
            try:
                with self._changed:
                    if job.cancel_event.is_set():
                        raise AgentCancelled("Tugas dihentikan oleh pengguna.")
                    job.status = "running"
                    job.started_at = _now()
                    job.progress = "Agent mulai"
                    job.progress_event = "run.started"
                    self._record_event_locked(job, "progress", job.progress)
                self._persist(job)
                publish_progress = partial(self._publish_progress, job)
                with self._project_lock(job):
                    result = self._run_goal(
                        job.goal,
                        self.settings,
                        **goal_kwargs(job.options),
                        on_event=publish_progress,
                        should_cancel=job.cancel_event.is_set,
                    )
                project_path = self.settings.workspace_dir.resolve() / result.project
                files = visible_project_files(project_path)
                with self._lock:
                    if job.cancel_event.is_set():
                        raise AgentCancelled("Tugas dihentikan oleh pengguna.")
                    job.achieved = bool(result.achieved)
                    job.reason = result.reason
                    job.project = result.project
                    job.iterations = len(result.iterations)
                    job.files_written = result.files_written
                    job.files = files
                    job.response = result.response
                    job.status = "completed" if result.achieved else "failed"
            except AgentCancelled:
                with self._lock:
                    job.status = "cancelled"
                    job.reason = "Tugas dihentikan oleh pengguna."
                    project_path = (
                        self.settings.workspace_dir.resolve() / job.project if job.project else None
                    )
                    if project_path is not None and project_path.is_dir():
                        job.files = visible_project_files(project_path)
            except GoalError as exc:
                with self._lock:
                    job.status = "failed"
                    job.error = str(exc)
                    job.error_public = "Goal tidak dapat diselesaikan."
                    job.reason = str(exc)
            except Exception as exc:  # noqa: BLE001 - worker harus tetap hidup
                with self._lock:
                    job.status = "failed"
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.error_public = "Worker mengalami error tak terduga."
                    job.reason = "Worker mengalami error tak terduga."
            finally:
                with self._changed:
                    job.finished_at = _now()
                    final_text = (
                        job.response
                        or job.error_public
                        or (job.reason if job.status == "cancelled" else "")
                        or "Coding agent belum dapat menyelesaikan tugas."
                    )
                    self._record_event_locked(
                        job,
                        job.status if job.status in TERMINAL_STATUSES else "failed",
                        final_text,
                    )
                self._persist(job)
                self._jobs_finished.labels(status=job.status).inc()
                self._job_duration.labels(status=job.status).observe(time.monotonic() - started)
                self._queue.task_done()


class _GoalHandler(BaseHTTPRequestHandler):
    service: AgentService
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - API BaseHTTPRequestHandler
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_metrics(self) -> None:
        body = self.service.prometheus_metrics()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_download(
        self,
        body: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> None:
        encoded_name = quote(filename, safe="")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded_name}")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _job_for_download(self, job_id: str) -> Job | None:
        job = self.service.get(job_id)
        if job is None:
            self._send(404, {"error": "Job coding agent tidak ditemukan."})
            return None
        if job.status not in TERMINAL_STATUSES:
            self._send(409, {"error": "File belum siap diunduh."})
            return None
        return job

    def _resolve_job_file(self, job: Job, relative_path: str) -> Path | None:
        if not relative_path or "\\" in relative_path or relative_path not in job.files:
            return None
        relative = PurePosixPath(relative_path)
        if (
            relative.is_absolute()
            or relative.as_posix() != relative_path
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            return None
        try:
            workspace = self.service.settings.workspace_dir.resolve(strict=True)
            project = (workspace / job.project).resolve(strict=True)
            project.relative_to(workspace)
            candidate = project.joinpath(*relative.parts)
            if candidate.is_symlink():
                return None
            target = candidate.resolve(strict=True)
            target.relative_to(project)
        except (OSError, ValueError):
            return None
        return target if target.is_file() else None

    def _download_job_file(self, job_id: str, relative_path: str) -> None:
        job = self._job_for_download(job_id)
        if job is None:
            return
        target = self._resolve_job_file(job, relative_path)
        if target is None:
            self._send(404, {"error": "File hasil tidak ditemukan."})
            return
        try:
            body = target.read_bytes()
        except OSError:
            self._send(404, {"error": "File hasil tidak ditemukan."})
            return
        self._send_download(
            body,
            filename=PurePosixPath(relative_path).name,
            content_type="application/octet-stream",
        )

    def _download_job_archive(self, job_id: str) -> None:
        job = self._job_for_download(job_id)
        if job is None:
            return
        files: list[tuple[str, Any]] = []
        total_size = 0
        for relative_path in job.files:
            target = self._resolve_job_file(job, relative_path)
            if target is None:
                self._send(404, {"error": "Daftar file hasil sudah berubah."})
                return
            try:
                total_size += target.stat().st_size
            except OSError:
                self._send(404, {"error": "Daftar file hasil sudah berubah."})
                return
            if total_size > _MAX_ARCHIVE_BYTES:
                self._send(413, {"error": "Ukuran proyek terlalu besar untuk arsip ZIP."})
                return
            files.append((relative_path, target))
        if not files:
            self._send(404, {"error": "Proyek tidak memiliki file untuk diunduh."})
            return

        archive = io.BytesIO()
        project_name = PurePosixPath(job.project).name
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for relative_path, target in files:
                bundle.write(target, f"{project_name}/{relative_path}")
        self._send_download(
            archive.getvalue(),
            filename=f"{project_name}.zip",
            content_type="application/zip",
        )

    def _authorized(self) -> bool:
        """Bearer token opsional (AGENT_API_TOKEN). Tanpa token, semua klien diizinkan."""
        token = self.service.settings.service_token
        if not token:
            return True
        header = self.headers.get("Authorization", "").strip()
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer":
            supplied = ""
        if hmac.compare_digest(supplied.strip().encode(), token.encode()):
            return True
        self._send(401, {"error": "token tidak valid atau tidak ada"})
        return False

    def do_GET(self) -> None:  # noqa: N802 - API BaseHTTPRequestHandler
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(200, {"status": "ok", **self.service.stats()})
        elif path == "/metrics":
            self._send_metrics()
        elif not self._authorized():
            return
        elif path.startswith("/goals/") and path.endswith("/files.zip"):
            job_id = path[len("/goals/") : -len("/files.zip")].rstrip("/")
            if not job_id or "/" in job_id:
                self._send(404, {"error": "job tidak ditemukan"})
            else:
                self._download_job_archive(job_id)
        elif path.startswith("/goals/") and "/files/" in path:
            job_part, relative_path = path[len("/goals/") :].split("/files/", 1)
            if not job_part or "/" in job_part:
                self._send(404, {"error": "job tidak ditemukan"})
            else:
                self._download_job_file(job_part, unquote(relative_path))
        elif path.startswith("/goals/") and path.endswith("/events"):
            job_id = path[len("/goals/") : -len("/events")].rstrip("/")
            if not job_id or "/" in job_id:
                self._send(404, {"error": "job tidak ditemukan"})
            else:
                self._stream_events(job_id)
        elif path == "/goals":
            self._send(200, {"jobs": [job.to_dict() for job in self.service.list_jobs()]})
        elif path.startswith("/goals/"):
            job = self.service.get(path[len("/goals/") :])
            if job is None:
                self._send(404, {"error": "job tidak ditemukan"})
            else:
                self._send(200, job.to_dict())
        else:
            self._send(404, {"error": "endpoint tidak ditemukan"})

    def _stream_events(self, job_id: str) -> None:
        if self.service.get(job_id) is None:
            self._send(404, {"error": "job tidak ditemukan"})
            return
        try:
            last_event_id = max(0, int(self.headers.get("Last-Event-ID", "0")))
        except ValueError:
            last_event_id = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        try:
            self.wfile.write(b"retry: 1500\n\n")
            self.wfile.flush()
            while True:
                batch = self.service.wait_for_events(job_id, last_event_id, 15.0)
                if batch is None:
                    return
                events, status, finished = batch
                for event in events:
                    payload = json.dumps(
                        {"text": event["text"], "created_at": event["created_at"]},
                        ensure_ascii=True,
                    )
                    message = f"id: {event['id']}\nevent: {event['event']}\ndata: {payload}\n\n"
                    self.wfile.write(message.encode("utf-8"))
                    self.wfile.flush()
                    last_event_id = event["id"]
                if finished and status in TERMINAL_STATUSES and not events:
                    return
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:  # noqa: N802 - API BaseHTTPRequestHandler
        if urlsplit(self.path).path != "/goals":
            self._send(404, {"error": "endpoint tidak ditemukan"})
            return
        if not self._authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"error": "Content-Length tidak valid"})
            return
        if length < 1 or length > MAX_BODY_BYTES:
            self._send(413, {"error": "body terlalu besar atau kosong"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, RecursionError):
            self._send(400, {"error": "body bukan JSON"})
            return
        goal = payload.get("goal") if isinstance(payload, dict) else None
        if not isinstance(goal, str) or not goal.strip():
            self._send(400, {"error": "field 'goal' wajib berupa teks"})
            return
        try:
            options = parse_goal_options(
                payload,
                iteration_limit=self.service.settings.max_goal_iterations_limit,
            )
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return
        try:
            job = self.service.submit(goal, options)
        except QueueFullError as exc:
            self._send(429, {"error": str(exc)})
            return
        self._send(
            202,
            {
                "id": job.id,
                "container_id": job.container_id,
                "status": job.status,
                "queue": self.service.stats(),
            },
        )

    def do_DELETE(self) -> None:  # noqa: N802 - API BaseHTTPRequestHandler
        path = urlsplit(self.path).path
        prefix = "/goals/"
        job_id = path.removeprefix(prefix)
        if not path.startswith(prefix) or not job_id or "/" in job_id:
            self._send(404, {"error": "job tidak ditemukan"})
            return
        if not self._authorized():
            return
        job = self.service.cancel(job_id)
        if job is None:
            self._send(404, {"error": "job tidak ditemukan"})
            return
        self._send(200, job.to_dict())


def create_server(settings: Settings, service: AgentService) -> ThreadingHTTPServer:
    handler = type("GoalHandler", (_GoalHandler,), {"service": service})
    return ThreadingHTTPServer((settings.service_host, settings.service_port), handler)


def serve(
    settings: Settings,
    *,
    service: AgentService | None = None,
    server: ThreadingHTTPServer | None = None,
) -> None:
    """Jalankan layanan sampai dihentikan (SIGINT/SIGTERM)."""
    active_service = service or AgentService(settings)
    active_server = server or create_server(settings, active_service)
    active_service.start()

    def shutdown(_signum: int, _frame: Any) -> None:
        threading.Thread(target=active_server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
    except ValueError:
        pass
    try:
        active_server.serve_forever()
    finally:
        active_service.stop()
        active_server.server_close()
