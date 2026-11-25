from dataclasses import replace
from itertools import count

import httpx
import pytest

from agent.config import SettingsError
from agent.service_client import ServiceError, fetch_job, get_job, post_goal, submit_goal


def test_submit_goal_waits_until_job_reaches_terminal_state(monkeypatch, settings):
    posts = []
    gets = []
    states = iter(
        [
            {"status": "queued"},
            {"status": "running"},
            {"status": "completed", "achieved": True, "project": "demo", "reason": "ok"},
        ]
    )

    def fake_post(base_url, goal, timeout_s):
        posts.append((base_url, goal, timeout_s))
        return {"id": "job-1", "status": "queued"}

    def fake_get(base_url, job_id, timeout_s):
        gets.append(job_id)
        return next(states)

    monkeypatch.setattr("agent.service_client.time.sleep", lambda _: None)
    result = submit_goal("Buat demo", settings, post=fake_post, get=fake_get)

    assert result["achieved"] is True
    assert posts[0][:2] == (settings.service_url, "Buat demo")
    assert gets == ["job-1", "job-1", "job-1"]


def test_submit_goal_reports_progress_only_when_it_changes(monkeypatch, settings):
    states = iter(
        [
            {"status": "running", "progress_seq": 1, "progress": "Agent dimulai"},
            {"status": "running", "progress_seq": 1, "progress": "Agent dimulai"},
            {"status": "completed", "progress_seq": 2, "progress": "Goal tercapai"},
        ]
    )
    updates = []
    monkeypatch.setattr("agent.service_client.time.sleep", lambda _: None)

    submit_goal(
        "Buat demo",
        settings,
        post=lambda *args: {"id": "job-progress"},
        get=lambda *args: next(states),
        on_progress=updates.append,
    )

    assert [item["progress"] for item in updates] == ["Agent dimulai", "Goal tercapai"]


def test_submit_goal_can_return_immediately(monkeypatch, settings):
    called = []
    monkeypatch.setattr(
        "agent.service_client.time.sleep", lambda _: pytest.fail("tidak boleh menunggu")
    )
    result = submit_goal(
        "Buat demo",
        settings,
        wait=False,
        post=lambda *args: {"id": "job-2", "status": "queued"},
        get=lambda *args: called.append(True),
    )
    assert result["id"] == "job-2"
    assert not called


def test_submit_goal_times_out(monkeypatch, settings):
    settings = replace(settings, service_submit_timeout_s=1)
    clock = count(0.0, 1.0)
    monkeypatch.setattr("agent.service_client.time.sleep", lambda _: None)
    monkeypatch.setattr("agent.service_client.time.monotonic", lambda: next(clock))
    with pytest.raises(ServiceError, match="belum selesai"):
        submit_goal(
            "Buat demo",
            settings,
            post=lambda *args: {"id": "job-3"},
            get=lambda *args: {"status": "queued"},
        )


def test_post_goal_wraps_connection_errors(monkeypatch, settings):
    class FailingClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr("agent.service_client.httpx.Client", FailingClient)
    with pytest.raises(ServiceError, match="Gagal mengirim goal"):
        post_goal(settings.service_url, "Buat demo", 1.0)


def test_invalid_service_settings_are_rejected(settings):
    with pytest.raises(SettingsError, match="AGENT_SERVICE_PORT"):
        replace(settings, service_port=0)
    with pytest.raises(SettingsError, match="SERVICE_SUBMIT_TIMEOUT_S"):
        replace(settings, service_submit_timeout_s=0)
    with pytest.raises(SettingsError, match="AGENT_INSTANCE_ID"):
        replace(settings, service_instance_id="invalid/id")


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    post_payload = {"id": "job-1", "status": "queued"}
    get_payload = {"id": "job-1", "status": "completed", "achieved": True}
    post_error = None
    get_error = None
    seen = []

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None, headers=None):
        self.seen.append(("POST", url, json, headers))
        if self.post_error:
            raise self.post_error
        return _Response(self.post_payload)

    def get(self, url, headers=None):
        self.seen.append(("GET", url, headers))
        if self.get_error:
            raise self.get_error
        return _Response(self.get_payload)


def test_post_goal_dan_get_job_lewat_http(monkeypatch, settings):
    monkeypatch.setattr("agent.service_client.httpx.Client", _Client)
    assert post_goal(settings.service_url, "Buat demo", 5.0)["id"] == "job-1"
    assert get_job(settings.service_url, "job-1", 5.0)["achieved"] is True


def test_http_error_menyertakan_detail_dari_layanan(monkeypatch, settings):
    class BadClient(_Client):
        post_error = httpx.HTTPStatusError(
            "400",
            request=httpx.Request("POST", "http://layanan/goals"),
            response=httpx.Response(400, json={"error": "goal wajib diisi"}),
        )

    monkeypatch.setattr("agent.service_client.httpx.Client", BadClient)
    with pytest.raises(ServiceError, match=r"HTTP 400: goal wajib diisi"):
        post_goal(settings.service_url, "", 5.0)


def test_http_error_tanpa_detail(monkeypatch, settings):
    class BadClient(_Client):
        get_error = httpx.HTTPStatusError(
            "500",
            request=httpx.Request("GET", "http://layanan/goals/x"),
            response=httpx.Response(500, text="boom"),
        )

    monkeypatch.setattr("agent.service_client.httpx.Client", BadClient)
    with pytest.raises(ServiceError, match="HTTP 500"):
        get_job(settings.service_url, "x", 5.0)


def test_get_job_wraps_connection_error(monkeypatch, settings):
    class BadClient(_Client):
        get_error = httpx.ConnectError("offline")

    monkeypatch.setattr("agent.service_client.httpx.Client", BadClient)
    with pytest.raises(ServiceError, match="Gagal mengambil status job"):
        get_job(settings.service_url, "x", 5.0)


def test_respons_bukan_objek_ditolak(monkeypatch, settings):
    class ArrayClient(_Client):
        post_payload = ["bukan objek"]
        get_payload = ["bukan objek"]

    monkeypatch.setattr("agent.service_client.httpx.Client", ArrayClient)
    with pytest.raises(ServiceError, match="Respons submit bukan objek JSON"):
        post_goal(settings.service_url, "demo", 5.0)
    with pytest.raises(ServiceError, match="Respons status bukan objek JSON"):
        get_job(settings.service_url, "x", 5.0)


def test_submit_goal_retry_polling_gangguan_sementara(monkeypatch, settings):
    states = iter(
        [
            ServiceError("network blink"),
            {"status": "completed", "achieved": True, "reason": "ok"},
        ]
    )

    def fake_get(*args):
        item = next(states)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("agent.service_client.time.sleep", lambda _: None)
    result = submit_goal(
        "Buat demo",
        settings,
        post=lambda *args: {"id": "job-retry"},
        get=fake_get,
    )
    assert result["achieved"] is True


def test_submit_goal_error_menyertakan_job_id(monkeypatch, settings):
    def fake_get(*args):
        raise ServiceError("offline")

    monkeypatch.setattr("agent.service_client.time.sleep", lambda _: None)
    with pytest.raises(ServiceError, match="job-gagal.*coding-agent job job-gagal"):
        submit_goal(
            "Buat demo",
            settings,
            post=lambda *args: {"id": "job-gagal"},
            get=fake_get,
        )


def test_submit_goal_mengirim_token_bearer(monkeypatch, settings):
    _Client.seen = []
    monkeypatch.setattr("agent.service_client.httpx.Client", _Client)
    token_settings = replace(settings, service_token="token-rahasia-32-karakter-lebih-panjang")
    result = submit_goal("Buat demo", token_settings, wait=False)

    assert result["id"] == "job-1"
    method, url, body, headers = _Client.seen[0]
    assert (method, body) == ("POST", {"goal": "Buat demo"})
    token = token_settings.service_token
    assert headers == {"Authorization": f"Bearer {token}"}

    assert fetch_job("job-1", token_settings)["achieved"] is True
    assert _Client.seen[1][2] == {"Authorization": f"Bearer {token}"}
