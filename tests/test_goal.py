import json
import stat
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage

from agent.config import SettingsError
from agent.goal import (
    GoalAcceptance,
    _project_fingerprint,
    resolve_project_dir,
    run_goal_loop,
)
from agent.schemas import CommandResult


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages):
        return next(self.responses)


def tool_call(name, args, call_id="call-1"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def judge_response(complete: bool, reason: str = "Penilaian otomatis.") -> AIMessage:
    return AIMessage(content=f'{{"complete": {str(complete).lower()}, "reason": "{reason}"}}')


def worker_rounds(count: int, path: str = "main.py") -> list[AIMessage]:
    rounds: list[AIMessage] = []
    for index in range(count):
        rounds.extend(
            [
                tool_call("list_dir", {"path": "."}, f"list-{index}"),
                tool_call(
                    "write_file",
                    {"path": path, "content": f"print({index})\n"},
                    f"write-{index}",
                ),
                AIMessage(content=f"Putaran {index + 1} selesai."),
            ]
        )
    return rounds


def test_resolve_project_dir_creates_unique_folder_and_honors_explicit_name(settings):
    first = resolve_project_dir(settings, "Build a Todo App!")
    assert first.name == "build-a-todo-app"

    second = resolve_project_dir(settings, "Build a Todo App!")
    assert second.name == "build-a-todo-app-2"

    explicit = resolve_project_dir(settings, "Build a Todo App!", "layanan-api")
    assert explicit.name == "layanan-api"
    assert explicit.exists()


def test_project_fingerprint_detects_same_size_content_change(settings):
    project = settings.workspace_dir / "fingerprint"
    project.mkdir(parents=True)
    target = project / "main.txt"
    target.write_text("abc")

    before = _project_fingerprint(project)
    target.write_text("xyz")

    assert _project_fingerprint(project) != before


def test_goal_default_iteration_limit_is_nine(settings):
    assert settings.max_goal_iterations == 9


def test_goal_loop_creates_project_and_stops_when_goal_is_achieved(settings):
    seen: list[str] = []

    def fake_verify(command, sandbox, actual_settings, cwd=".", timeout_s=None):
        seen.append(command)
        return CommandResult(argv=[command], exit_code=0, output="ok")

    result = run_goal_loop(
        "Buat kalkulator CLI",
        settings,
        acceptance=GoalAcceptance(
            verification_commands=("python -m py_compile main.py",),
            required_files=("main.py",),
        ),
        llm=ScriptedModel(worker_rounds(1)),
        judge=ScriptedModel([judge_response(True, "File dan verifikasi sesuai tujuan.")]),
        run_verify=fake_verify,
    )

    assert result.achieved
    assert result.project == "buat-kalkulator-cli"
    assert len(result.iterations) == 1
    assert result.files_written == 1
    assert result.verifications[0].result.ok
    assert seen == ["python -m py_compile main.py"]
    assert (settings.workspace_dir / "buat-kalkulator-cli" / "main.py").read_text() == "print(0)\n"
    log_path = settings.logs_dir / f"run-{result.session_id}.jsonl"
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert {event["event"] for event in events} >= {
        "goal.iteration.started",
        "goal.verification.completed",
        "judge.request",
        "judge.response",
        "goal.acceptance.checked",
    }
    assert any(event.get("goal") == "Buat kalkulator CLI" for event in events)


def test_goal_loop_retries_when_machine_check_fails(monkeypatch, settings):
    outcomes = iter(
        [
            CommandResult(argv=["verify"], exit_code=1, output="assertion failed"),
            CommandResult(argv=["verify"], exit_code=0, output="1 passed"),
        ]
    )
    commands: list[str] = []

    def fake_verify(command, sandbox, actual_settings, cwd=".", timeout_s=None):
        commands.append(command)
        return next(outcomes)

    result = run_goal_loop(
        "Perbaiki modul",
        settings,
        project_name="service",
        acceptance=GoalAcceptance(verification_commands=("python -m pytest",)),
        llm=ScriptedModel(worker_rounds(2)),
        judge=ScriptedModel(
            [judge_response(False, "Verifikasi masih gagal."), judge_response(True, "Lulus.")]
        ),
        run_verify=fake_verify,
    )

    assert result.achieved
    assert len(result.iterations) == 2
    assert commands == ["python -m pytest", "python -m pytest"]
    assert "Verifikasi masih gagal" in result.iterations[0].acceptance.reason


def test_required_file_gates_completion_even_when_judge_agrees(settings):
    result = run_goal_loop(
        "Buat aplikasi",
        settings,
        project_name="app",
        max_iterations=1,
        acceptance=GoalAcceptance(required_files=("README.md",)),
        llm=ScriptedModel(worker_rounds(1)),
        judge=ScriptedModel([judge_response(True, "Menurut ringkasan sudah selesai.")]),
    )

    assert not result.achieved
    assert "Batas 1 putaran" in result.reason
    check = result.iterations[0].acceptance
    assert check.required_files_ok is False
    assert check.missing_files == ("README.md",)
    assert check.judge_ok is True
    assert check.complete is False


def test_goal_loop_stops_when_no_progress_is_made(settings):
    worker = ScriptedModel(
        [
            tool_call("list_dir", {"path": "."}, "list-1"),
            AIMessage(content="Belum ada perubahan."),
            tool_call("list_dir", {"path": "."}, "list-2"),
            AIMessage(content="Masih belum ada perubahan."),
            tool_call("list_dir", {"path": "."}, "list-3"),
            AIMessage(content="Tetap belum ada perubahan."),
        ]
    )
    judge = ScriptedModel([judge_response(False) for _ in range(3)])

    result = run_goal_loop(
        "Selesaikan tugas tanpa perubahan",
        settings,
        project_name="stuck",
        llm=worker,
        judge=judge,
        allow_no_changes=True,
    )

    assert not result.achieved
    assert "Tidak ada kemajuan" in result.reason
    assert len(result.iterations) == settings.goal_stuck_limit + 1


def test_invalid_goal_loop_settings_are_rejected(settings):
    with pytest.raises(SettingsError, match="MAX_GOAL_ITER"):
        replace(settings, max_goal_iterations=0)
    with pytest.raises(SettingsError, match="GOAL_STUCK_LIMIT"):
        replace(settings, goal_stuck_limit=0)
