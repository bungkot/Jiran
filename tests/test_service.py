import io
import json
import socket
import threading
import time
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from agent.agent import AgentCancelled
from agent.service import AgentService, QueueFullError, create_server, parse_goal_options

_TOKEN = "token-rahasia-32-karakter-lebih-panjang"  # noqa: S105 - token dummy untuk test


def _free_port() -> int:
    """Cari port kosong agar test tidak bergantung pada service standby di host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(service: AgentService, job_id: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.get(job_id)
        if job and job.status in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job tidak selesai tepat waktu")


def test_service_processes_goals_serially(settings):
    active = 0
    maximum = 0
    guard = threading.Lock()

    def fake_run(goal, actual_settings, **kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return SimpleNamespace(
            achieved=True,
            reason="selesai",
            project="demo",
            iterations=(),
            files_written=1,
            response="ok",
        )

    service = AgentService(settings, run_goal=fake_run)
    service.start()
    try:
        first = service.submit("Goal satu")
        second = service.submit("Goal dua")
        assert wait_for(service, first.id).status == "completed"
        assert wait_for(service, second.id).status == "completed"
        assert maximum == 1
        assert service.stats()["running"] == 0
    finally:
        service.stop()


def test_service_includes_visible_project_files_in_job(settings):
    project = settings.workspace_dir / "demo"
    project.mkdir(parents=True)
    (project / "README.md").write_text("# Demo\n", encoding="utf-8")
    (project / ".env").write_text("PRIVATE=value\n", encoding="utf-8")

    def fake_run(goal, actual_settings, **kwargs):
        return SimpleNamespace(
            achieved=True,
            reason="selesai",
            project="demo",
            iterations=(),
            files_written=1,
            response="Proyek siap.",
        )

    service = AgentService(settings, run_goal=fake_run)
    service.start()
    try:
        job = service.submit("Buat demo")
        completed = wait_for(service, job.id)
        assert completed.files == ["README.md"]
        assert completed.to_dict()["files"] == ["README.md"]
    finally:
        service.stop()


def test_service_instances_serialize_jobs_for_same_project(settings):
    first_started = threading.Event()
    release_first = threading.Event()
    entered = []
    guard = threading.Lock()

    def fake_run(goal, actual_settings, **kwargs):
        with guard:
            entered.append(goal)
        if goal == "pertama":
            first_started.set()
            release_first.wait(timeout=3)
        return SimpleNamespace(
            achieved=True,
            reason="selesai",
            project="demo",
            iterations=(),
            files_written=1,
            response="ok",
        )

    first = AgentService(replace(settings, service_instance_id="agent-1"), fake_run)
    second = AgentService(replace(settings, service_instance_id="agent-2"), fake_run)
    first.start()
    second.start()
    try:
        first_job = first.submit("pertama", {"project": "demo"})
        assert first_started.wait(timeout=1)
        second_job = second.submit("kedua", {"project": "demo"})
        time.sleep(0.1)
        assert entered == ["pertama"]

        release_first.set()
        assert wait_for(first, first_job.id).status == "completed"
        assert wait_for(second, second_job.id).status == "completed"
        assert entered == ["pertama", "kedua"]
    finally:
        release_first.set()
        first.stop()
        second.stop()


def test_service_reports_failed_goal_and_persists_metadata(settings):
    def fake_run(goal, actual_settings, **kwargs):
        return SimpleNamespace(
            achieved=False,
            reason="belum tercapai",
            project="demo",
            iterations=(),
            files_written=0,
            response="",
        )

    service = AgentService(settings, run_goal=fake_run)
    service.start()
    try:
        job = wait_for(service, service.submit("Goal gagal").id)
        assert job.status == "failed"
        assert not job.achieved
        assert job.reason == "belum tercapai"
        path = settings.logs_dir / "jobs" / f"job-{job.id}.json"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            data = json.loads(path.read_text())
            if data["status"] == "failed":
                break
            time.sleep(0.01)
        assert data["status"] == "failed"
        assert "Goal gagal" not in json.dumps(data)
    finally:
        service.stop()


def test_service_publishes_safe_progress_events(settings):
    def fake_run(goal, actual_settings, *, on_event, **kwargs):
        on_event(
            {
                "event": "tool.completed",
                "tool": "write_file",
                "ok": True,
                "arguments": {"content": "secret source"},
            }
        )
        return SimpleNamespace(
            achieved=True,
            reason="selesai",
            project="demo",
            iterations=(),
            files_written=1,
            response="ok",
        )

    service = AgentService(settings, run_goal=fake_run)
    service.start()
    try:
        job = wait_for(service, service.submit("Goal satu").id)
        assert job.progress == "Tool write_file selesai"
        assert job.progress_event == "tool.completed"
        metadata = (settings.logs_dir / "jobs" / f"job-{job.id}.json").read_text()
        assert "secret source" not in metadata
    finally:
        service.stop()


def test_http_api_streams_safe_progress_and_final_response(settings):
    def fake_run(goal, actual_settings, *, on_event, **kwargs):
        on_event(
            {
                "event": "tool.completed",
                "tool": "write_file",
                "ok": True,
                "arguments": {"content": "private source"},
            }
        )
        return SimpleNamespace(
            achieved=True,
            reason="selesai",
            project="demo",
            iterations=(),
            files_written=1,
            response="Implementasi selesai.",
        )

    service = AgentService(settings, run_goal=fake_run)
    server = create_server(replace(settings, service_port=_free_port()), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    service.start()
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with httpx.Client(timeout=5.0) as client:
            submitted = client.post(f"{base}/goals", json={"goal": "Buat demo"})
            assert submitted.status_code == 202
            job_id = submitted.json()["id"]

            with client.stream(
                "GET",
                f"{base}/goals/{job_id}/events",
                headers={"Last-Event-ID": "1"},
            ) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                body = "".join(response.iter_text())

        assert "id: 1\n" not in body
        assert "event: progress" in body
        assert "event: completed" in body
        assert "Implementasi selesai." in body
        assert "private source" not in body
    finally:
        server.shutdown()
        service.stop()
        server.server_close()


def test_http_api_accepts_concurrent_submissions_and_reports_status(settings):
    service = AgentService(
        settings,
        run_goal=lambda goal, actual_settings, **kwargs: SimpleNamespace(
            achieved=True,
            reason="ok",
            project="demo",
            iterations=(),
            files_written=1,
            response="selesai",
        ),
    )
    server = create_server(replace(settings, service_port=_free_port()), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    service.start()
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with httpx.Client(timeout=5.0) as client:
            health = client.get(f"{base}/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

            submitted = client.post(f"{base}/goals", json={"goal": "Buat demo"})
            assert submitted.status_code == 202
            job_id = submitted.json()["id"]
            assert job_id.startswith(f"{settings.service_instance_id}_")
            assert len(job_id.rsplit("_", 1)[1]) == 32
            assert submitted.json()["container_id"] == service.container_id

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                detail = client.get(f"{base}/goals/{job_id}")
                assert detail.status_code == 200
                if detail.json()["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            assert detail.json()["achieved"] is True
            assert detail.json()["container_id"] == service.container_id

            metrics = client.get(f"{base}/metrics")
            assert metrics.status_code == 200
            assert "text/plain" in metrics.headers["content-type"]
            assert b"jiran_agent_jobs_submitted_total 1.0" in metrics.content
            assert b'jiran_agent_jobs_finished_total{status="completed"} 1.0' in metrics.content
            assert b"jiran_agent_queue_size 0.0" in metrics.content

            invalid = client.post(f"{base}/goals", json={"goal": "  "})
            assert invalid.status_code == 400
            assert client.get(f"{base}/goals/tidak-ada").status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        service.stop()


def test_http_api_cancels_running_job(settings):
    started = threading.Event()

    def fake_run(goal, actual_settings, *, should_cancel, **kwargs):
        started.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not should_cancel():
            time.sleep(0.01)
        if should_cancel():
            raise AgentCancelled("Tugas dihentikan oleh pengguna.")
        raise AssertionError("Cancellation request was not delivered")

    service = AgentService(settings, run_goal=fake_run)
    server = create_server(replace(settings, service_port=_free_port()), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    service.start()
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with httpx.Client(timeout=5.0) as client:
            submitted = client.post(f"{base}/goals", json={"goal": "Buat demo"})
            assert submitted.status_code == 202
            job_id = submitted.json()["id"]
            assert started.wait(timeout=1)

            cancelled = client.delete(f"{base}/goals/{job_id}")
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelling"
            assert wait_for(service, job_id).status == "cancelled"
            assert client.delete(f"{base}/goals/tidak-ada").status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        service.stop()


def test_http_api_downloads_visible_files_and_zip(settings):
    project = settings.workspace_dir / "download-demo"
    project.mkdir(parents=True)
    (project / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (project / "README.md").write_text("# Download demo\n", encoding="utf-8")
    (project / ".env").write_text("PRIVATE=value\n", encoding="utf-8")

    def fake_run(goal, actual_settings, **kwargs):
        return SimpleNamespace(
            achieved=True,
            reason="selesai",
            project="download-demo",
            iterations=(),
            files_written=2,
            response="Proyek siap.",
        )

    service = AgentService(settings, run_goal=fake_run)
    server = create_server(replace(settings, service_port=_free_port()), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    service.start()
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with httpx.Client(timeout=5.0) as client:
            submitted = client.post(f"{base}/goals", json={"goal": "Buat demo"})
            assert submitted.status_code == 202
            job_id = submitted.json()["id"]
            assert wait_for(service, job_id).status == "completed"

            file_response = client.get(f"{base}/goals/{job_id}/files/main.py")
            assert file_response.status_code == 200
            assert file_response.content == b"print('hello')\n"
            assert file_response.headers["content-disposition"].endswith("filename*=UTF-8''main.py")

            readme_response = client.get(f"{base}/goals/{job_id}/files/README.md")
            assert readme_response.status_code == 200
            assert readme_response.content == b"# Download demo\n"
            assert readme_response.headers["content-disposition"].endswith(
                "filename*=UTF-8''README.md"
            )

            hidden_response = client.get(f"{base}/goals/{job_id}/files/.env")
            assert hidden_response.status_code == 404

            archive_response = client.get(f"{base}/goals/{job_id}/files.zip")
            assert archive_response.status_code == 200
            assert archive_response.headers["content-type"] == "application/zip"
            with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
                assert archive.namelist() == [
                    "download-demo/README.md",
                    "download-demo/main.py",
                ]
                assert archive.read("download-demo/main.py") == b"print('hello')\n"
    finally:
        server.shutdown()
        server.server_close()
        service.stop()


def test_service_menolak_bearer_huruf_kecil(monkeypatch, settings):
    port = _free_port()
    secured = replace(settings, service_host="127.0.0.1", service_port=port, service_token=_TOKEN)
    service = AgentService(
        secured,
        run_goal=lambda *a, **k: SimpleNamespace(
            achieved=True,
            reason="ok",
            project="demo",
            iterations=(),
            files_written=0,
            response="",
        ),
    )
    server = create_server(secured, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    service.start()
    thread.start()
    try:
        reply = httpx.post(
            f"http://127.0.0.1:{port}/goals",
            json={"goal": "demo"},
            headers={"Authorization": f"bearer {_TOKEN}"},
            timeout=5,
        )
        assert reply.status_code == 202
        wait_for(service, reply.json()["id"])
    finally:
        server.shutdown()
        server.server_close()
        service.stop()


def test_service_antrean_penuh_ditolak(settings):
    service = AgentService(settings, run_goal=lambda *a, **k: None)
    for index in range(100):
        service.submit(f"Goal {index}")
    with pytest.raises(QueueFullError, match="Antrean goal penuh"):
        service.submit("Goal ke-101")


def test_parse_goal_options_membatasi_iterasi():
    assert parse_goal_options({"max_iterations": 50})["max_iterations"] == 50
    with pytest.raises(ValueError, match="1..50"):
        parse_goal_options({"max_iterations": 1000})
    with pytest.raises(ValueError, match="1..10"):
        parse_goal_options({"max_iterations": 11}, iteration_limit=10)


def test_error_detail_hanya_di_log_bukan_di_api(settings):
    def boom(*args, **kwargs):
        raise ValueError("path rahasia /data/rahasia")

    service = AgentService(settings, run_goal=boom)
    service.start()
    job = wait_for(service, service.submit("Goal").id)
    service.stop()

    assert job.status == "failed"
    assert "path rahasia" in job.error  # detail internal
    assert job.to_dict()["error"] == "Worker mengalami error tak terduga."
    metadata = (settings.logs_dir / "jobs" / f"job-{job.id}.json").read_text()
    assert "path rahasia" in metadata  # audit trail lokal tetap lengkap


def test_service_rejects_empty_goal(settings):
    service = AgentService(settings, run_goal=lambda *args: None)
    with pytest.raises(ValueError, match="Goal"):
        service.submit("   ")


def _post(settings, path, body, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.post(
        f"http://{settings.service_host}:{settings.service_port}{path}",
        json=body,
        headers=headers,
        timeout=5,
    )


def test_service_token_dan_opsi_goal(settings):
    seen = []

    def fake_run(goal, actual_settings, **kwargs):
        seen.append((goal, kwargs))
        return SimpleNamespace(
            achieved=True,
            reason="ok",
            project="demo",
            iterations=(),
            files_written=0,
            response="",
        )

    port = _free_port()
    secured = replace(settings, service_host="127.0.0.1", service_port=port, service_token=_TOKEN)
    service = AgentService(secured, run_goal=fake_run)
    service.start()
    server = create_server(secured, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert httpx.get(f"http://127.0.0.1:{port}/health", timeout=5).status_code == 200
        assert _post(secured, "/goals", {"goal": "x"}).status_code == 401
        assert _post(secured, "/goals", {"goal": "x"}, "salah").status_code == 401
        invalid = _post(secured, "/goals", {"goal": "x", "max_iterations": 0}, _TOKEN)
        assert invalid.status_code == 400
        reply = _post(
            secured,
            "/goals",
            {"goal": "g", "project": "api", "verify": ["pytest -q"], "require_files": ["main.py"]},
            _TOKEN,
        )
        assert reply.status_code == 202
        wait_for(service, reply.json()["id"])
        goal, kwargs = seen[0]
        assert goal == "g" and kwargs["project_name"] == "api"
        assert kwargs["acceptance"].verification_commands == ("pytest -q",)
        assert kwargs["acceptance"].required_files == ("main.py",)
    finally:
        server.shutdown()
        server.server_close()
        service.stop()
