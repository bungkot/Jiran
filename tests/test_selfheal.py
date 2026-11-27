import json
import urllib.parse

import pytest

from scripts import selfheal


def test_selfheal_watches_single_agent_and_ollama():
    assert selfheal.AGENT_SERVICES == {"agent-1"}
    assert selfheal.DEFAULT_WATCH == ("agent-1", "ollama")
    assert set(selfheal.TARGET_URLS) == {"agent-1", "ollama"}


def test_poll_state_checks_single_agent(monkeypatch):
    checked = []
    monkeypatch.setattr(selfheal, "check", lambda url, *_args: checked.append(url) or True)

    state = selfheal.poll_state(("agent-1",))

    assert state == [{"name": "agent-1", "status": "up"}]
    assert checked == ["http://agent-1:8765/health"]


def test_report_incident_sends_severity(monkeypatch):
    sent = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def urlopen(request, timeout):
        sent.append((json.loads(request.data), timeout))
        return Response()

    monkeypatch.setattr(selfheal.urllib.request, "urlopen", urlopen)

    assert selfheal.report_incident(
        "http://itsm",
        "token",
        "service:agent-1",
        "Service agent-1 down",
        "Service tidak sehat.",
        True,
        severity="high",
    )
    assert sent == [
        (
            {
                "fingerprint": "service:agent-1",
                "title": "Service agent-1 down",
                "detail": "Service tidak sehat.",
                "active": True,
                "severity": "high",
            },
            5.0,
        )
    ]


def test_strict_reporting_raises_when_itsm_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        selfheal,
        "poll_state",
        lambda _watch: [{"name": "agent-1", "status": "up"}, {"name": "ollama", "status": "up"}],
    )
    monkeypatch.setattr(selfheal, "poll_resources", lambda _url: {})
    monkeypatch.setattr(selfheal, "report_incident", lambda *_args, **_kwargs: False)

    with pytest.raises(RuntimeError, match="service:agent-1, service:ollama"):
        selfheal.run_once(
            ("agent-1", "ollama"),
            "http://ollama:11434",
            "model",
            "/var/run/docker.sock",
            "http://prometheus:9090",
            itsm_url="http://itsm:3000",
            itsm_token="token",
            strict_reporting=True,
        )


def test_strict_reporting_raises_when_container_restart_fails(monkeypatch):
    monkeypatch.setattr(
        selfheal,
        "poll_state",
        lambda _watch: [{"name": "ollama", "status": "down"}],
    )
    monkeypatch.setattr(selfheal, "poll_resources", lambda _url: {})
    monkeypatch.setattr(selfheal, "report_incident", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(selfheal, "_restart_if_target", lambda *_args, **_kwargs: False)

    with pytest.raises(RuntimeError, match="Container restart failed for: ollama"):
        selfheal.run_once(
            ("ollama",),
            "http://ollama:11434",
            "model",
            "/var/run/docker.sock",
            "http://prometheus:9090",
            itsm_url="http://itsm:3000",
            itsm_token="token",
            strict_reporting=True,
        )


def test_container_lookup_scopes_to_compose_project(monkeypatch):
    requests = []

    class Response:
        status = 200

        def read(self):
            return json.dumps([{"Names": ["/jiran-agent-3-1"]}]).encode()

    class Connection:
        def __init__(self, _sock_path):
            pass

        def request(self, method, path):
            requests.append((method, path))

        def getresponse(self):
            return Response()

    monkeypatch.setattr(selfheal, "UnixHTTPConnection", Connection)

    result = selfheal.find_container_name("/var/run/docker.sock", "agent-3", project="jiran")

    assert result == "jiran-agent-3-1"
    method, path = requests[0]
    query = urllib.parse.urlsplit(path).query
    filters = json.loads(urllib.parse.parse_qs(query)["filters"][0])
    assert method == "GET"
    assert filters["label"] == [
        "com.docker.compose.service=agent-3",
        "com.docker.compose.project=jiran",
    ]
