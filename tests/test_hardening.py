from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import tool

from agent import main as cli
from agent.agent import (
    AgentError,
    _enforce_context_limit,
    _extract_tool_calls,
    _tool_result,
    run_agent,
)
from agent.config import (
    Settings,
    SettingsError,
    _env_bool,
    _env_float,
    _env_int,
)
from agent.schemas import CommandResult
from agent.tools.container_runner import _docker_command, run_command_in_container
from agent.tools.sandbox import Sandbox, SandboxError


class _Response:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


def test_extract_tool_calls_menolak_json_dalam_prosa():
    text = 'Berikut: {"name": "write_file", "arguments": {"path": "x", "content": "y"}}'
    assert _extract_tool_calls(_Response(content=text)) == []


def test_extract_tool_calls_menolak_json_berikut_teks():
    text = '{"name": "list_dir", "arguments": {"path": "."}} lalu lanjut teks'
    assert _extract_tool_calls(_Response(content=text)) == []


def test_extract_tool_calls_menerima_json_dalam_blok_prosa():
    text = (
        "Saya akan memeriksa proyek.\n\n"
        "```json\n"
        '{"name": "read_file", "arguments": {"path": "src/calc.py"}}\n'
        "```\n\n"
        "Kemudian saya akan menulis perbaikannya.\n\n"
        "```json\n"
        '{"name": "write_file", "arguments": {"path": "src/calc.py", "content": "ok"}}\n'
        "```\n"
    )
    calls = _extract_tool_calls(_Response(content=text))
    assert [call["name"] for call in calls] == ["read_file", "write_file"]


def test_extract_tool_calls_mendukung_array_json():
    text = (
        '[{"name":"list_dir","arguments":{"path":"."}},'
        '{"name":"read_file","arguments":{"path":"README.md"}}]'
    )
    calls = _extract_tool_calls(_Response(content=text))
    assert [call["name"] for call in calls] == ["list_dir", "read_file"]


def test_extract_tool_calls_normalisasi_args_string_native():
    response = _Response(
        tool_calls=[{"name": "write_file", "args": '{"path":"x","content":"y"}', "id": "1"}]
    )
    calls = _extract_tool_calls(response)
    assert calls[0]["args"] == {"path": "x", "content": "y"}


def test_tool_result_menangkap_exception_tak_terduga():
    @tool
    def boom(x: str) -> str:
        """Selalu gagal."""
        raise RuntimeError("boom")

    text, ok = _tool_result(boom, {"x": "1"})
    assert not ok and "RuntimeError" in text


def test_model_retry_saat_gagal_sementara(monkeypatch, settings):
    class FlakyModel:
        def __init__(self):
            self.responses = iter([TimeoutError("timeout"), AIMessage(content="Selesai.")])
            self.calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, messages):
            self.calls += 1
            item = next(self.responses)
            if isinstance(item, Exception):
                raise item
            return item

    monkeypatch.setattr("agent.agent.time.sleep", lambda _: None)
    settings = replace(settings, llm_max_retries=1, max_agent_steps=1)
    project = settings.workspace_dir
    project.mkdir(parents=True)
    model = FlakyModel()

    result = run_agent("Tulis sesuatu.", project, settings, llm=model, allow_no_changes=True)

    assert result.steps == 1
    assert model.calls == 2
    assert "model.retry" in (settings.logs_dir / f"run-{result.session_id}.jsonl").read_text()


def test_model_gagal_setelah_retry_habis(monkeypatch, settings):
    class BrokenModel:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, messages):
            self.calls += 1
            raise TimeoutError("timeout")

    monkeypatch.setattr("agent.agent.time.sleep", lambda _: None)
    settings = replace(settings, llm_max_retries=2)
    project = settings.workspace_dir
    project.mkdir(parents=True)

    with pytest.raises(AgentError, match="berkomunikasi"):
        run_agent("Tulis sesuatu.", project, settings, llm=BrokenModel())


def test_enforce_context_limit_memotong_pesan_lama():
    messages = [
        SystemMessage(content="sistem"),
        *[AIMessage(content=f"Pesan lama {i} " + "x" * 1000) for i in range(11)],
        AIMessage(content="Pesan terakhir"),
    ]
    _enforce_context_limit(messages, limit=1200)
    assert len(messages[1].content) < 600
    assert messages[-1].content == "Pesan terakhir"


def test_settings_load_env_angka_invalid(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_S", "abc")
    with pytest.raises(SettingsError, match="LLM_TIMEOUT_S"):
        Settings.load()


def test_settings_validasi_nilai():
    with pytest.raises(SettingsError):
        Settings(llm_timeout_s=0)
    with pytest.raises(SettingsError):
        Settings(llm_max_retries=-1)
    with pytest.raises(SettingsError):
        Settings(max_context_chars=10)


def test_env_numeric_and_boolean_helpers(monkeypatch):
    monkeypatch.delenv("TEST_INT", raising=False)
    assert _env_int("TEST_INT", 7) == 7
    monkeypatch.setenv("TEST_INT", "11")
    assert _env_int("TEST_INT", 7) == 11

    monkeypatch.setenv("TEST_FLOAT", "1.25")
    assert _env_float("TEST_FLOAT", 1.0) == 1.25

    monkeypatch.setenv("TEST_BOOL", "yes")
    assert _env_bool("TEST_BOOL", False) is True
    monkeypatch.setenv("TEST_BOOL", "off")
    assert _env_bool("TEST_BOOL", True) is False

    monkeypatch.setenv("TEST_INT", "bukan-angka")
    with pytest.raises(SettingsError, match="TEST_INT"):
        _env_int("TEST_INT", 7)
    monkeypatch.setenv("TEST_BOOL", "mungkin")
    with pytest.raises(SettingsError, match="TEST_BOOL"):
        _env_bool("TEST_BOOL", False)


def test_settings_workspace_kosong_ditolak(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", "   ")
    with pytest.raises(SettingsError, match="WORKSPACE_DIR"):
        Settings.load()


def test_cli_konfigurasi_invalid(monkeypatch, capsys):
    def boom():
        raise SettingsError("contoh")

    monkeypatch.setattr(cli.Settings, "load", boom)
    assert cli.main(["preflight"]) == 1
    assert "Konfigurasi tidak valid" in capsys.readouterr().out


def test_docker_command_mengunci_swap(monkeypatch, settings):
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "package.json").write_text("{}")
    monkeypatch.setattr("agent.tools.container_runner.shutil.which", lambda name: "/usr/bin/docker")
    sandbox = Sandbox(settings.workspace_dir)

    argv, _ = _docker_command("npm test", sandbox, settings, ".")

    assert "--memory-swap" in argv
    assert argv[argv.index("--memory-swap") + 1] == f"{settings.sandbox_memory_mb}m"


def test_run_command_menolak_timeout_kurang_dari_satu(settings):
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "package.json").write_text("{}")
    sandbox = Sandbox(settings.workspace_dir)

    with pytest.raises(SandboxError, match="timeout_s"):
        run_command_in_container("true", sandbox, settings, timeout_s=0)


def test_force_remove_container_setelah_stop_gagal(monkeypatch, settings):
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "package.json").write_text("{}")
    sandbox = Sandbox(settings.workspace_dir)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1 if "stop" in argv else 0, stderr="")

    monkeypatch.setattr("agent.tools.container_runner.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        "agent.tools.container_runner.run_argv",
        lambda *a, **k: CommandResult(argv=["docker"], timed_out=True),
    )
    monkeypatch.setattr("agent.tools.container_runner.subprocess.run", fake_run)

    result = run_command_in_container("true", sandbox, settings)

    assert result.timed_out
    assert any("rm" in argv for argv in calls)
