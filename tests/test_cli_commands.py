import json

import pytest

from agent import main as cli
from agent.agent import AgentError, AgentResult, Verification
from agent.config import SettingsError
from agent.container_client import ContainerError, ContainerGoalResult
from agent.goal import GoalError
from agent.schemas import (
    CheckStatus,
    CommandResult,
    LLMPingResult,
    PreflightCheck,
    PreflightReport,
)
from agent.service_client import ServiceError


def _ok_report():
    return PreflightReport(checks=[PreflightCheck(name="docker", status=CheckStatus.OK)])


def _failed_report():
    return PreflightReport(
        checks=[
            PreflightCheck(
                name="docker", status=CheckStatus.FAIL, detail="mati", hint="jalankan docker"
            )
        ]
    )


def _settings_and_preflight(monkeypatch, settings, report=None):
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(cli, "run_agent_preflight", lambda _: report or _ok_report())


def test_main_tanpa_argumen_menampilkan_bantuan(capsys):
    assert cli.main([]) == 1
    assert "usage:" in capsys.readouterr().out


def test_main_help_tidak_membutuhkan_konfigurasi(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_konfigurasi_invalid_gagal_sebelum_perintah(monkeypatch, capsys):
    def boom():
        raise SettingsError("contoh")

    monkeypatch.setattr(cli.Settings, "load", boom)
    assert cli.main(["job", "job-1"]) == 1
    assert "Konfigurasi tidak valid" in capsys.readouterr().out


def test_semua_gate_dijalankan_berurutan(monkeypatch, settings):
    _settings_and_preflight(monkeypatch, settings)
    monkeypatch.setattr(cli, "get_llm", lambda _: object())
    monkeypatch.setattr(
        cli,
        "ping_llm",
        lambda *args, **kwargs: LLMPingResult(ok=True, latency_s=0.1, response="pong"),
    )
    monkeypatch.setattr(
        cli, "check_docker", lambda: PreflightCheck(name="docker", status=CheckStatus.OK)
    )

    assert cli.main(["all"]) == 0


def test_gate_berhenti_pada_preflight_gagal(monkeypatch, settings, capsys):
    _settings_and_preflight(monkeypatch, settings, _failed_report())
    monkeypatch.setattr(cli, "ping_llm", lambda *a, **k: pytest.fail("tidak boleh dipanggil"))

    assert cli.main(["all"]) == 1
    assert "Preflight: GAGAL" in capsys.readouterr().out


def test_ping_gagal_dilaporkan(monkeypatch, settings, capsys):
    _settings_and_preflight(monkeypatch, settings)
    monkeypatch.setattr(cli, "get_llm", lambda _: object())
    monkeypatch.setattr(
        cli,
        "ping_llm",
        lambda *a, **k: LLMPingResult(ok=False, error="offline"),
    )

    assert cli.main(["ping"]) == 1
    assert "GATE Fase 1: GAGAL" in capsys.readouterr().out


def test_tools_gagal_bila_docker_tidak_siap(monkeypatch, settings, capsys):
    _settings_and_preflight(monkeypatch, settings)
    monkeypatch.setattr(
        cli, "check_docker", lambda: PreflightCheck(name="docker", status=CheckStatus.FAIL)
    )

    assert cli.main(["tools"]) == 1
    assert "Tools: GAGAL" in capsys.readouterr().out


def test_serve_meneruskan_settings(monkeypatch, settings):
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    seen = []
    monkeypatch.setattr(cli, "serve", seen.append)

    assert cli.main(["serve"]) == 0
    assert seen == [settings]


def test_job_status_menentukan_exit_code(monkeypatch, settings, capsys):
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    jobs = iter(
        [
            {"id": "1", "status": "completed", "achieved": True, "reason": "ok"},
            {"id": "2", "status": "completed", "achieved": False, "reason": "belum"},
            {"id": "3", "status": "failed", "error": "boom"},
            {"id": "4", "status": "running", "progress": "Agent dimulai"},
        ]
    )
    monkeypatch.setattr(cli, "fetch_job", lambda *args: next(jobs))

    assert cli.main(["job", "1"]) == 0
    assert "TERCAPAI" in capsys.readouterr().out
    assert cli.main(["job", "2"]) == 1
    assert "BELUM TERCAPAI" in capsys.readouterr().out
    assert cli.main(["job", "3"]) == 1
    assert cli.main(["job", "4"]) == 1  # masih berjalan, belum boleh dianggap sukses
    assert "Progres: Agent dimulai" in capsys.readouterr().out


def test_job_gagal_diambil(monkeypatch, settings, capsys):
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)

    def boom(*args):
        raise ServiceError("tidak terhubung")

    monkeypatch.setattr(cli, "fetch_job", boom)
    assert cli.main(["job", "x"]) == 1
    assert "Gagal: tidak terhubung" in capsys.readouterr().out


def test_submit_meneruskan_opsi_dan_no_wait(monkeypatch, settings, capsys):
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    seen = {}
    monkeypatch.setattr(
        cli,
        "submit_goal",
        lambda goal, actual_settings, **kwargs: (
            seen.update(goal=goal, kwargs=kwargs) or {"id": "job-9", "status": "queued"}
        ),
    )

    code = cli.main(
        [
            "submit",
            "--project",
            "api",
            "--verify",
            "pytest -q",
            "--require-file",
            "main.py",
            "--max-iterations",
            "4",
            "--allow-no-change",
            "--no-wait",
            "Buat API",
        ]
    )

    assert code == 0
    assert seen["goal"] == "Buat API"
    assert seen["kwargs"]["options"] == {
        "project": "api",
        "verify": ["pytest -q"],
        "require_files": ["main.py"],
        "max_iterations": 4,
        "allow_no_changes": True,
    }
    assert seen["kwargs"]["wait"] is False
    assert "job-9" in capsys.readouterr().out


def test_submit_gagal_dilaporkan(monkeypatch, settings, capsys):
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)

    def boom(*args, **kwargs):
        raise ServiceError("layanan mati")

    monkeypatch.setattr(cli, "submit_goal", boom)
    assert cli.main(["submit", "Buat demo"]) == 1
    assert "Gagal: layanan mati" in capsys.readouterr().out


def test_goal_json_preflight_gagal(monkeypatch, settings, capsys):
    _settings_and_preflight(monkeypatch, settings, _failed_report())
    assert cli.main(["goal", "--json", "Buat demo"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "achieved": False,
        "error": "preflight agent gagal",
    }


def test_goal_json_menangkap_goal_error(monkeypatch, settings, capsys):
    _settings_and_preflight(monkeypatch, settings)

    def boom(*args, **kwargs):
        raise GoalError("iterasi habis")

    monkeypatch.setattr(cli, "run_goal_loop", boom)
    assert cli.main(["goal", "--json", "Buat demo"]) == 1
    assert json.loads(capsys.readouterr().out) == {"achieved": False, "error": "iterasi habis"}


def test_goal_container_error(monkeypatch, settings, capsys):
    _settings_and_preflight(monkeypatch, settings)

    def boom(*args, **kwargs):
        raise ContainerError("docker mati")

    monkeypatch.setattr(cli, "run_container_goal", boom)
    assert cli.main(["goal", "--container", "--json", "Buat demo"]) == 1
    assert json.loads(capsys.readouterr().out) == {"achieved": False, "error": "docker mati"}


def test_goal_container_tanpa_payload(monkeypatch, settings, capsys):
    _settings_and_preflight(monkeypatch, settings)
    monkeypatch.setattr(
        cli,
        "run_container_goal",
        lambda *a, **k: ContainerGoalResult("c1", False, 0, False, "tanpa json", None),
    )
    assert cli.main(["goal", "--container", "--json", "Buat demo"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "achieved": False,
        "error": "output JSON tidak ditemukan",
    }


def test_run_menangkap_error_proyek_dan_agent(monkeypatch, settings, capsys):
    _settings_and_preflight(monkeypatch, settings)
    assert cli.main(["run", "--project", "tidak-ada", "Kerjakan"]) == 1
    assert "Gagal:" in capsys.readouterr().out

    project = settings.workspace_dir / "demo"
    project.mkdir(parents=True)

    def boom(*args, **kwargs):
        raise AgentError("model gagal")

    monkeypatch.setattr(cli, "run_agent", boom)
    assert cli.main(["run", "--project", "demo", "Kerjakan"]) == 1
    assert "model gagal" in capsys.readouterr().out


def test_run_menampilkan_verifikasi_gagal(monkeypatch, settings, capsys):
    _settings_and_preflight(monkeypatch, settings)
    project = settings.workspace_dir / "demo"
    project.mkdir(parents=True)
    result = AgentResult(
        session_id="s1",
        response="Ringkasan.",
        steps=1,
        tool_calls=1,
        verifications=(
            Verification("pytest -q", CommandResult(argv=["docker"], exit_code=1, output="FAIL")),
        ),
        finished=True,
        files_written=1,
    )
    monkeypatch.setattr(cli, "run_agent", lambda *a, **k: result)

    assert cli.main(["run", "--project", "demo", "Kerjakan"]) == 1
    output = capsys.readouterr().out
    assert "Verifikasi" in output
    assert "FAIL" in output


def test_container_build(monkeypatch, settings, capsys):
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/docker")
    seen = {}
    monkeypatch.setattr(
        cli,
        "run_argv",
        lambda argv, **kwargs: (
            seen.update(argv=argv) or type("R", (), {"ok": True, "output": "built"})()
        ),
    )

    assert cli.main(["container-build", "--image", "custom:tag"]) == 0
    assert seen["argv"][1:4] == ["build", "-t", "custom:tag"]
    assert "built" in capsys.readouterr().out


def test_container_build_tanpa_docker(monkeypatch, settings, capsys):
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)

    assert cli.main(["container-build"]) == 1
    assert "Docker CLI tidak ditemukan" in capsys.readouterr().out


def test_main_returns_error_when_no_subcommand_and_empty_goal(monkeypatch, settings):
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    assert cli.main(["   "]) == 1
