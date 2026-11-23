from dataclasses import replace
from itertools import count

import httpx
import pytest

from agent.config import SettingsError
from agent.service_client import ServiceError, post_goal, submit_goal


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
