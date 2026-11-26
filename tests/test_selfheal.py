import json
import urllib.parse

from scripts import selfheal


def test_selfheal_includes_third_agent():
    assert "agent-3" in selfheal.AGENT_SERVICES
    assert "agent-3" in selfheal.DEFAULT_WATCH
    assert selfheal.TARGET_URLS["agent-3"] == "http://agent-3:8765/health"


def test_stopped_autoscaled_agent_is_not_reported_as_failed(monkeypatch):
    monkeypatch.setattr(selfheal, "container_is_running", lambda *_args: False)
    monkeypatch.setattr(selfheal, "check", lambda *_args: False)

    state = selfheal.poll_state(("agent-3",), sock_path="/unused", project="jiran")

    assert state == [{"name": "agent-3", "status": "stopped"}]


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
