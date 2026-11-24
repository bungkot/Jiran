import json
import socket
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

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
        if job and job.status in {"completed", "failed"}:
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

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                detail = client.get(f"{base}/goals/{job_id}")
                assert detail.status_code == 200
                if detail.json()["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            assert detail.json()["achieved"] is True

            invalid = client.post(f"{base}/goals", json={"goal": "  "})
            assert invalid.status_code == 400
            assert client.get(f"{base}/goals/tidak-ada").status_code == 404
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
