import subprocess
from types import SimpleNamespace

import pytest

from scripts import benchmark_models as bench


def _listed(stdout: str, code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=stdout)


def test_tanpa_docker_fallback_ke_host(monkeypatch):
    monkeypatch.setattr(bench.shutil, "which", lambda _: None)
    assert bench._agent_command("m:1") == [bench.sys.executable, "-m", "agent.main"]


def test_env_container_kustom_dipakai(monkeypatch):
    monkeypatch.setattr(bench.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setenv("BENCH_AGENT_CONTAINER", "custom-agent")

    def fail_docker(*args, **kwargs):
        pytest.fail("tidak perlu query docker bila nama eksplisit diberikan")

    monkeypatch.setattr(bench, "run", fail_docker)
    command = bench._agent_command("m:1")
    assert command[-2:] == ["custom-agent", "coding-agent"]


@pytest.mark.parametrize("name", ["agent", "jiran"])
def test_container_terdeteksi_dari_docker_ps(monkeypatch, name):
    monkeypatch.setattr(bench.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.delenv("BENCH_AGENT_CONTAINER", raising=False)
    monkeypatch.setattr(bench, "run", lambda *a, **k: _listed(f"other\n{name}\n"))

    command = bench._agent_command("m:1")
    assert command[-2] == name


def test_tidak_ada_container_fallback_ke_host(monkeypatch):
    monkeypatch.setattr(bench.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.delenv("BENCH_AGENT_CONTAINER", raising=False)
    monkeypatch.setattr(bench, "run", lambda *a, **k: _listed(""))
    assert bench._agent_command("m:1") == [bench.sys.executable, "-m", "agent.main"]


def test_docker_ps_gagal_fallback_ke_host(monkeypatch):
    monkeypatch.setattr(bench.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.delenv("BENCH_AGENT_CONTAINER", raising=False)
    monkeypatch.setattr(bench, "run", lambda *a, **k: _listed("", 1))
    assert bench._agent_command("m:1") == [bench.sys.executable, "-m", "agent.main"]


def test_docker_ps_timeout_fallback_ke_host(monkeypatch):
    monkeypatch.setattr(bench.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.delenv("BENCH_AGENT_CONTAINER", raising=False)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker", 15)

    monkeypatch.setattr(bench, "run", timeout)
    assert bench._agent_command("m:1") == [bench.sys.executable, "-m", "agent.main"]
