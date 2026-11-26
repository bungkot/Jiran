import json

from scripts import autoscale


def _configure_autoscaler(monkeypatch, polls):
    containers = {
        name: {"id": f"id-{name}", "state": "running", "name": name} for name in autoscale.SERVICES
    }
    actions = []
    monkeypatch.setattr(autoscale, "list_agent_containers", lambda: containers)
    monkeypatch.setattr(autoscale, "poll_service", lambda name: polls[name])

    def action(kind, container_id, *, stop_timeout=2):
        actions.append((kind, container_id))
        return True

    monkeypatch.setattr(autoscale, "container_action", action)
    return actions


def test_keeps_one_worker_for_one_active_job(monkeypatch):
    polls = {
        "agent-1": {"up": 1, "queued": 0, "running": 1},
        "agent-2": {"up": 0, "queued": 0, "running": 0},
        "agent-3": {"up": 0, "queued": 0, "running": 0},
    }
    actions = _configure_autoscaler(monkeypatch, polls)

    autoscale.run_once({}, max_scale=3)

    assert actions == []


def test_starts_worker_for_concurrent_queued_job(monkeypatch):
    polls = {
        "agent-1": {"up": 1, "queued": 1, "running": 1},
        "agent-2": {"up": 0, "queued": 0, "running": 0},
        "agent-3": {"up": 0, "queued": 0, "running": 0},
    }
    actions = _configure_autoscaler(monkeypatch, polls)

    autoscale.run_once({}, max_scale=3)

    assert actions == [("start", "id-agent-2")]


def test_stops_idle_workers_when_no_jobs_remain(monkeypatch):
    polls = {
        "agent-1": {"up": 1, "queued": 0, "running": 0},
        "agent-2": {"up": 1, "queued": 0, "running": 0},
        "agent-3": {"up": 1, "queued": 0, "running": 0},
    }
    actions = _configure_autoscaler(monkeypatch, polls)

    autoscale.run_once({}, max_scale=3)

    assert actions == [("stop", "id-agent-3"), ("stop", "id-agent-2")]


def test_keeps_recently_used_worker_during_polling_grace(monkeypatch):
    polls = {
        "agent-1": {"up": 1, "queued": 0, "running": 1},
        "agent-2": {"up": 1, "queued": 0, "running": 1},
        "agent-3": {"up": 0, "queued": 0, "running": 0},
    }
    actions = _configure_autoscaler(monkeypatch, polls)
    now = [100.0]
    monkeypatch.setattr(autoscale.time, "monotonic", lambda: now[0])
    state = {}

    autoscale.run_once(state, max_scale=3, drain_grace_s=60)
    polls["agent-1"]["running"] = 0
    polls["agent-2"]["running"] = 0
    now[0] = 101.0
    autoscale.run_once(state, max_scale=3, drain_grace_s=60)

    assert actions == []

    now[0] = 161.0
    autoscale.run_once(state, max_scale=3, drain_grace_s=60)

    assert actions == [("stop", "id-agent-2")]


def test_container_discovery_ignores_other_compose_projects(monkeypatch):
    class Response:
        def read(self):
            return json.dumps(
                [
                    {
                        "Id": "ours",
                        "State": "running",
                        "Names": ["/jiran-agent-1-1"],
                        "Labels": {
                            "com.docker.compose.service": "agent-1",
                            "com.docker.compose.project": "jiran",
                        },
                    },
                    {
                        "Id": "other",
                        "State": "running",
                        "Names": ["/other-agent-1-1"],
                        "Labels": {
                            "com.docker.compose.service": "agent-1",
                            "com.docker.compose.project": "other",
                        },
                    },
                ]
            ).encode()

    class Connection:
        def __init__(self, _sock_path):
            pass

        def request(self, _method, _path):
            pass

        def getresponse(self):
            return Response()

    monkeypatch.setattr(autoscale, "UnixHTTPConnection", Connection)
    monkeypatch.setattr(autoscale, "SERVICES", ("agent-1",))
    monkeypatch.setattr(autoscale, "COMPOSE_PROJECT", "jiran")

    containers = autoscale.list_agent_containers()

    assert containers["agent-1"]["id"] == "ours"
