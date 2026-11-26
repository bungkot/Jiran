import httpx

from agent import preflight as pf
from agent.schemas import CheckStatus, PreflightCheck, PreflightReport


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._data


def test_python_ok():
    assert pf.check_python((3, 8)).status == CheckStatus.OK


def test_python_terlalu_lama():
    assert pf.check_python((99, 0)).status == CheckStatus.FAIL


def test_ollama_mati(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(pf.httpx, "get", boom)
    check, models = pf.check_ollama("http://localhost:11434")
    assert check.status == CheckStatus.FAIL and models == []


def test_ollama_hidup(monkeypatch):
    monkeypatch.setattr(
        pf.httpx, "get", lambda *a, **k: FakeResp({"models": [{"name": "qwen2.5-coder:7b"}]})
    )
    check, models = pf.check_ollama("http://x")
    assert check.ok and models == ["qwen2.5-coder:7b"]


def test_model_terpasang_dan_normalisasi_latest():
    assert pf.check_model("llama3.1", ["llama3.1:latest"]).ok
    assert pf.check_model("qwen2.5-coder:7b", ["qwen2.5-coder:7b"]).ok


def test_model_tidak_ada():
    c = pf.check_model("foo:1b", ["bar:2b"])
    assert c.status == CheckStatus.FAIL and "ollama pull foo:1b" in c.hint


def test_model_tools_didukung(monkeypatch):
    monkeypatch.setattr(
        pf.httpx, "post", lambda *a, **k: FakeResp({"capabilities": ["completion", "tools"]})
    )
    assert pf.check_model_tools("http://x", "m").status == CheckStatus.OK


def test_model_tools_tidak_didukung_hanya_warn(monkeypatch):
    monkeypatch.setattr(
        pf.httpx, "post", lambda *a, **k: FakeResp({"capabilities": ["completion"]})
    )
    assert pf.check_model_tools("http://x", "m").status == CheckStatus.WARN


def test_model_tools_capabilities_tidak_dilaporkan(monkeypatch):
    monkeypatch.setattr(pf.httpx, "post", lambda *a, **k: FakeResp({}))
    assert pf.check_model_tools("http://x", "m").status == CheckStatus.WARN


def test_workspace_bisa_ditulis(settings):
    assert pf.check_workspace(settings).status == CheckStatus.OK


def test_report_gate_warn_tidak_memblokir():
    r = PreflightReport(
        checks=[
            PreflightCheck(name="a", status=CheckStatus.OK),
            PreflightCheck(name="b", status=CheckStatus.WARN),
        ]
    )
    assert r.ok and len(r.warnings) == 1


def test_report_gate_fail_memblokir():
    r = PreflightReport(checks=[PreflightCheck(name="a", status=CheckStatus.FAIL)])
    assert not r.ok and len(r.failures) == 1


def test_ollama_respons_tidak_valid_tidak_crash(monkeypatch):
    for payload in ([], {"models": None}, {"models": ["x"]}):
        monkeypatch.setattr(pf.httpx, "get", lambda *a, _p=payload, **k: FakeResp(_p))
        check, models = pf.check_ollama("http://x")
        assert check.status == CheckStatus.FAIL and models == []


def test_model_tools_respons_tidak_valid_hanya_warn(monkeypatch):
    monkeypatch.setattr(pf.httpx, "post", lambda *a, **k: FakeResp([]))
    check = pf.check_model_tools("http://x", "m")
    assert check.status == CheckStatus.WARN and "tidak valid" in check.detail


def test_model_tools_capabilities_bukan_list_hanya_warn(monkeypatch):
    monkeypatch.setattr(pf.httpx, "post", lambda *a, **k: FakeResp({"capabilities": "tools"}))
    check = pf.check_model_tools("http://x", "m")
    assert check.status == CheckStatus.WARN


def test_agent_preflight_does_not_require_node_or_npm(monkeypatch, settings):
    monkeypatch.setattr(
        pf,
        "check_docker",
        lambda: PreflightCheck(name="docker", status=CheckStatus.OK),
    )
    monkeypatch.setattr(
        pf, "check_workspace", lambda _: PreflightCheck(name="workspace", status=CheckStatus.OK)
    )
    monkeypatch.setattr(
        pf, "check_python", lambda: PreflightCheck(name="python", status=CheckStatus.OK)
    )
    monkeypatch.setattr(
        pf.httpx,
        "get",
        lambda *args, **kwargs: FakeResp({"models": [{"name": settings.ollama_model}]}),
    )
    monkeypatch.setattr(
        pf.httpx,
        "post",
        lambda *args, **kwargs: FakeResp({"capabilities": ["completion", "tools"]}),
    )

    report = pf.run_agent_preflight(settings)

    assert report.ok
    assert {check.name for check in report.checks} == {
        "python",
        "workspace",
        "docker",
        "ollama",
        f"model:{settings.ollama_model}",
        f"model-tools:{settings.ollama_model}",
    }


def test_agent_preflight_requires_tool_capable_model(monkeypatch, settings):
    monkeypatch.setattr(
        pf, "check_docker", lambda: PreflightCheck(name="docker", status=CheckStatus.OK)
    )
    monkeypatch.setattr(
        pf, "check_workspace", lambda _: PreflightCheck(name="workspace", status=CheckStatus.OK)
    )
    monkeypatch.setattr(
        pf, "check_python", lambda: PreflightCheck(name="python", status=CheckStatus.OK)
    )
    monkeypatch.setattr(
        pf.httpx,
        "get",
        lambda *args, **kwargs: FakeResp({"models": [{"name": settings.ollama_model}]}),
    )
    monkeypatch.setattr(
        pf.httpx,
        "post",
        lambda *args, **kwargs: FakeResp({"capabilities": ["completion"]}),
    )

    report = pf.run_agent_preflight(settings)

    assert not report.ok
    assert report.failures[-1].name.startswith("model-tools:")
