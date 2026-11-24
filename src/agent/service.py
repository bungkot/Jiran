"""Layanan goal standby: menerima banyak permintaan, memproses satu per satu.

HTTP server melayani submit/status secara konkuren, tetapi hanya ada satu worker
yang menjalankan ``run_goal_loop``. Dengan demikian beberapa klien boleh mengirim
goal bersamaan tanpa dua proyek dikerjakan dalam waktu yang sama.
"""

from __future__ import annotations

import hmac
import json
import queue
import signal
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .config import Settings
from .goal import GoalAcceptance, GoalError, run_goal_loop
from .progress import summarize_event

TERMINAL_STATUSES = frozenset({"completed", "failed"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


MAX_BODY_BYTES = 32_768
_MAX_VERIFY = 10
_MAX_REQUIRED_FILES = 50
_MAX_COMMAND_CHARS = 4000
_MAX_QUEUED_JOBS = 100
_DEFAULT_ITERATION_LIMIT = 50


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
    status: str = "queued"
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    achieved: bool = False
    reason: str = ""
    project: str = ""
    iterations: int = 0
    files_written: int = 0
    response: str = ""
    error: str = ""  # detail internal; hanya ditulis ke log/disk
    error_public: str = ""  # pesan aman untuk klien HTTP
    progress: str = ""
    progress_event: str = ""
    progress_seq: int = 0
    options: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
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
        self._queue: queue.Queue[Job] = queue.Queue(maxsize=_MAX_QUEUED_JOBS)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._jobs_dir = settings.logs_dir.resolve() / "jobs"
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._work, name="goal-worker", daemon=True)

    def start(self) -> None:
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker.is_alive():
            self._worker.join(timeout=5.0)

    def submit(self, goal: str, options: dict[str, Any] | None = None) -> Job:
        target = goal.strip()
        if not target:
            raise ValueError("Goal tidak boleh kosong.")
        with self._lock:
            running = sum(item.status == "running" for item in self._jobs.values())
            if self._queue.qsize() + running >= _MAX_QUEUED_JOBS:
                raise QueueFullError("Antrean goal penuh; coba lagi nanti.")
            job = Job(
                id=uuid.uuid4().hex,
                goal=target,
                created_at=_now(),
                options=dict(options or {}),
            )
            self._jobs[job.id] = job
            self._queue.put(job)
        self._persist(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return Job(**job.__dict__) if job else None

    def list_jobs(self) -> list[Job]:
        with self._lock:
            return [Job(**job.__dict__) for job in self._jobs.values()]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            running = sum(job.status == "running" for job in self._jobs.values())
        return {"queued": self._queue.qsize(), "running": running}

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
        with self._lock:
            job.progress = message
            job.progress_event = str(event.get("event", ""))
            job.progress_seq += 1
        self._persist(job)

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    job.status = "running"
                    job.started_at = _now()
                    job.progress = "Agent mulai"
                    job.progress_event = "run.started"
                    job.progress_seq += 1
                self._persist(job)
                publish_progress = partial(self._publish_progress, job)
                result = self._run_goal(
                    job.goal,
                    self.settings,
                    **goal_kwargs(job.options),
                    on_event=publish_progress,
                )
                with self._lock:
                    job.achieved = bool(result.achieved)
                    job.reason = result.reason
                    job.project = result.project
                    job.iterations = len(result.iterations)
                    job.files_written = result.files_written
                    job.response = result.response
                    job.status = "completed" if result.achieved else "failed"
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
                with self._lock:
                    job.finished_at = _now()
                self._persist(job)
                self._queue.task_done()


class _GoalHandler(BaseHTTPRequestHandler):
    service: AgentService

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - API BaseHTTPRequestHandler
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        elif not self._authorized():
            return
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
        self._send(202, {"id": job.id, "status": job.status, "queue": self.service.stats()})


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
