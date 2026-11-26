import json
import urllib.parse

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
