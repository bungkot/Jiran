import json

from agent import main as cli
from agent.agent import AgentResult, Verification
from agent.container_client import ContainerGoalRequest, ContainerGoalResult
from agent.goal import AcceptanceCheck, GoalAcceptance, GoalIteration, GoalResult
from agent.schemas import CheckStatus, CommandResult, PreflightCheck, PreflightReport


def test_run_cli_routes_any_project_and_requested_verification(monkeypatch, settings):
    project = settings.workspace_dir / "service"
    project.mkdir(parents=True)
    calls = {}
    expected_result = AgentResult(
        session_id="session-1",
        response="Updated the service.",
        steps=2,
        tool_calls=2,
        verifications=(
            Verification(
                "python -m pytest",
                CommandResult(argv=["docker"], exit_code=0, output="2 passed"),
            ),
        ),
        finished=True,
        files_written=1,
    )
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(
        cli,
        "run_agent_preflight",
        lambda _: PreflightReport(checks=[PreflightCheck(name="docker", status=CheckStatus.OK)]),
    )

    def fake_run_agent(prompt, project_root, actual_settings, **kwargs):
        calls.update(
            prompt=prompt,
            project_root=project_root,
            settings=actual_settings,
            kwargs=kwargs,
        )
        return expected_result

    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    result = cli.main(
        [
            "run",
            "--project",
            "service",
            "--verify",
            "python -m pytest",
            "Fix the parser bug.",
        ]
    )

    assert result == 0
    assert calls["prompt"] == "Fix the parser bug."
    assert calls["project_root"] == project.resolve()
    assert calls["kwargs"]["verification_commands"] == ["python -m pytest"]


def test_goal_cli_creates_project_and_routes_acceptance(monkeypatch, settings):
    calls = {}
    check = AcceptanceCheck(
        complete=True,
        reason="Tujuan tercapai.",
        verification_ok=True,
        required_files_ok=True,
        judge_ok=True,
    )
    expected = GoalResult(
        goal="Buat layanan API",
        project="layanan-api",
        session_id="goal-1",
        iterations=(
            GoalIteration(
                index=1,
                result=AgentResult("worker-1", "Selesai.", 2, 2, (), True, 3),
                acceptance=check,
            ),
        ),
        achieved=True,
        reason="Tujuan tercapai.",
    )
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(
        cli,
        "run_agent_preflight",
        lambda _: PreflightReport(checks=[PreflightCheck(name="docker", status=CheckStatus.OK)]),
    )

    def fake_goal_loop(goal, actual_settings, **kwargs):
        calls.update(goal=goal, settings=actual_settings, kwargs=kwargs)
        return expected

    monkeypatch.setattr(cli, "run_goal_loop", fake_goal_loop)

    result = cli.main(
        [
            "goal",
            "--project",
            "layanan-api",
            "--verify",
            "python -m pytest",
            "--require-file",
            "main.py",
            "--max-iterations",
            "3",
            "Buat layanan API",
        ]
    )

    assert result == 0
    assert calls["goal"] == "Buat layanan API"
    assert calls["kwargs"]["project_name"] == "layanan-api"
    assert calls["kwargs"]["max_iterations"] == 3
    assert calls["kwargs"]["acceptance"] == GoalAcceptance(
        verification_commands=("python -m pytest",),
        required_files=("main.py",),
    )


def test_goal_cli_container_mode_prints_json_and_returns_status(monkeypatch, settings, capsys):
    calls = {}
    payload = {"achieved": True, "reason": "Tujuan tercapai.", "project": "demo"}
    expected = ContainerGoalResult(
        container="coding-agent-goal-1",
        achieved=True,
        exit_code=0,
        timed_out=False,
        output="",
        payload=payload,
    )
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(
        cli,
        "run_agent_preflight",
        lambda _: PreflightReport(checks=[PreflightCheck(name="docker", status=CheckStatus.OK)]),
    )
    monkeypatch.setattr(
        cli,
        "run_container_goal",
        lambda request, actual_settings, **kwargs: (
            calls.update(request=request, settings=actual_settings) or expected
        ),
    )

    code = cli.main(
        [
            "goal",
            "--container",
            "--json",
            "--project",
            "demo",
            "--verify",
            "python -m pytest",
            "Buat demo",
        ]
    )

    assert code == 0
    assert calls["request"] == ContainerGoalRequest(
        goal="Buat demo",
        project="demo",
        verification_commands=("python -m pytest",),
    )
    assert json.loads(capsys.readouterr().out) == payload


def test_default_cli_sends_only_the_goal_to_standby_service(monkeypatch, settings):
    calls = {}
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(
        cli,
        "submit_goal",
        lambda goal, actual_settings, **kwargs: (
            calls.update(goal=goal, settings=actual_settings)
            or {
                "id": "job-1",
                "status": "completed",
                "achieved": True,
                "project": "demo",
                "iterations": 2,
                "files_written": 3,
                "reason": "Tujuan tercapai.",
                "response": "Selesai.",
            }
        ),
    )

    code = cli.main(["Buat", "kalkulator", "CLI"])

    assert code == 0
    assert calls["goal"] == "Buat kalkulator CLI"


def test_cli_progress_hides_prompt_and_model_response(capsys):
    with cli._CLIProgress() as progress:
        progress.event(
            {
                "event": "model.request",
                "step": 1,
                "messages": [{"content": "rahasia di prompt"}],
            }
        )
        assert progress._status is not None
        progress.event(
            {
                "event": "model.response",
                "content": "kode rahasia dari model",
                "tool_calls": [],
            }
        )
        progress.event(
            {
                "event": "tool.started",
                "tool": "write_file",
                "arguments": {"path": "main.py", "content": "rahasia file"},
            }
        )
        progress.event(
            {
                "event": "tool.completed",
                "tool": "write_file",
                "ok": True,
            }
        )

    output = capsys.readouterr().err
    assert "Respons model diterima" in output
    assert "Tool write_file selesai" in output
    assert "rahasia" not in output


def test_goal_cli_json_mode_prints_local_result(monkeypatch, settings, capsys):
    expected = GoalResult(
        goal="Buat demo",
        project="demo",
        session_id="goal-2",
        iterations=(
            GoalIteration(
                index=1,
                result=AgentResult("worker-1", "Selesai.", 2, 2, (), True, 1),
                acceptance=AcceptanceCheck(True, "ok", True, True, True),
            ),
        ),
        achieved=True,
        reason="ok",
    )
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(
        cli,
        "run_agent_preflight",
        lambda _: PreflightReport(checks=[PreflightCheck(name="docker", status=CheckStatus.OK)]),
    )
    monkeypatch.setattr(cli, "run_goal_loop", lambda *args, **kwargs: expected)

    code = cli.main(["goal", "--json", "Buat demo"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["achieved"] is True
    assert payload["iterations"] == 1
    assert payload["files_written"] == 1
