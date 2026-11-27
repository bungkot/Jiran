import json
import stat
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage

from agent.agent import AgentCancelled, AgentError, run_agent
from agent.schemas import CommandResult


def test_agent_does_not_treat_error_read_as_successful_inspection(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    (project / ".env").write_text("SECRET=value")
    model = ScriptedModel(
        [
            tool_call("read_file", {"path": ".env"}),
            tool_call("list_dir", {"path": "."}),
            tool_call("write_file", {"path": "main.py", "content": "print('ok')\n"}),
            AIMessage(content="Aman: secret tidak dibaca."),
        ]
    )

    result = run_agent("Buat file aman.", project, settings, llm=model)

    assert result.ok
    assert (project / "main.py").read_text() == "print('ok')\n"


def test_agent_honors_cancellation_before_model_request(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    model = ScriptedModel([])

    with pytest.raises(AgentCancelled, match="dihentikan"):
        run_agent(
            "Buat file aman.",
            project,
            settings,
            llm=model,
            should_cancel=lambda: True,
        )

    assert model.invoked_tools == []


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.bound_tools = []
        self.invoked_tools = []

    def bind_tools(self, tools, **kwargs):
        names = [item.name for item in tools]
        self.bound_tools.append(names)
        return BoundScriptedModel(self, names, kwargs.get("tool_choice"))


class BoundScriptedModel:
    def __init__(self, parent, tool_names, tool_choice):
        self.parent = parent
        self.tool_names = tool_names
        self.tool_choice = tool_choice

    def invoke(self, messages):
        self.parent.invoked_tools.append((self.tool_names, self.tool_choice))
        return next(self.parent.responses)


def tool_call(name, args, call_id="call-1"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def test_agent_logs_full_model_and_tool_exchange_with_private_permissions(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    model = ScriptedModel(
        [
            tool_call("list_dir", {"path": "."}),
            tool_call(
                "write_file",
                {
                    "path": "src/main.go",
                    "content": "package main\nfunc main() {}\n",
                },
            ),
            AIMessage(content="Added the Go entry point."),
        ]
    )

    result = run_agent(
        "Create a minimal Go entry point.",
        project,
        settings,
        llm=model,
        response_language="English",
    )

    assert result.ok and not result.verified
    assert result.tool_calls == 2
    assert result.files_written == 1
    assert len(model.bound_tools) == 1
    assert "write_file" in model.bound_tools[0]
    assert model.invoked_tools == [(model.bound_tools[0], None)] * 3
    assert (project / "src/main.go").read_text() == "package main\nfunc main() {}\n"
    log_path = next(settings.logs_dir.glob("run-*.jsonl"))
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert any(
        event["event"] == "model.request"
        and "Create a minimal Go entry point." in json.dumps(event)
        for event in events
    )
    request_event = next(event for event in events if event["event"] == "model.request")
    system_prompt = next(
        message["content"] for message in request_event["messages"] if message["type"] == "system"
    )
    normalized_prompt = " ".join(system_prompt.split())
    assert "If the request is in Indonesian, respond in Indonesian" in normalized_prompt
    assert "Use English when the user's language is unclear or mixed" in normalized_prompt
    assert "MANDATORY RESPONSE LANGUAGE FOR THIS RUN: English." in normalized_prompt
    assert any(
        event["event"] == "model.response" and "package main" in json.dumps(event)
        for event in events
    )
    tool_event = next(event for event in events if event["event"] == "tool.completed")
    assert tool_event["tool"] == "list_dir"
    write_event = next(
        event
        for event in events
        if event["event"] == "tool.completed" and event["tool"] == "write_file"
    )
    assert write_event["arguments"]["content"] == "package main\nfunc main() {}\n"
    assert write_event["result"].startswith("OK: menulis")


def test_agent_requests_summary_after_one_empty_final_response(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    model = ScriptedModel(
        [
            tool_call("list_dir", {"path": "."}),
            tool_call("write_file", {"path": "main.py", "content": "print('ok')\n"}),
            AIMessage(content=""),
            AIMessage(content="Created main.py and completed the requested change."),
        ]
    )

    result = run_agent("Create a small Python entry point.", project, settings, llm=model)

    assert result.ok
    assert result.response == "Created main.py and completed the requested change."
    assert result.steps == 4
    events = [
        json.loads(line)
        for line in (settings.logs_dir / f"run-{result.session_id}.jsonl").read_text().splitlines()
    ]
    assert any(event["event"] == "model.empty_response" for event in events)
    retry_request = next(
        event for event in events if event["event"] == "model.request" and event["step"] == 4
    )
    retry_prompt = retry_request["messages"][-1]["content"]
    assert "language of the user's original request" in retry_prompt
    assert "Do not call tools again" in retry_prompt


def test_agent_does_not_retry_repeated_empty_final_responses(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    model = ScriptedModel(
        [
            tool_call("list_dir", {"path": "."}),
            tool_call("write_file", {"path": "main.py", "content": "print('ok')\n"}),
            AIMessage(content=""),
            AIMessage(content=""),
        ]
    )

    with pytest.raises(AgentError, match="tanpa ringkasan"):
        run_agent("Create a small Python entry point.", project, settings, llm=model)


def test_agent_keeps_read_and_write_tools_available_until_change_is_applied(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    (project / "README.md").write_text("Existing project")
    model = ScriptedModel(
        [
            tool_call("read_file", {"path": "README.md"}),
            tool_call("write_file", {"path": "main.py", "content": "print('ready')\n"}),
            AIMessage(content="Added the CLI entry point."),
        ]
    )

    result = run_agent("Add a CLI entry point.", project, settings, llm=model)

    assert result.ok
    assert len(model.bound_tools) == 1
    assert "read_file" in model.bound_tools[0]
    assert "write_file" in model.bound_tools[0]
    assert (project / "main.py").exists()


def test_agent_can_inspect_multiple_files_before_editing(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    (project / "README.md").write_text("Use the configured app name.")
    (project / "settings.toml").write_text('app_name = "starter"\n')
    model = ScriptedModel(
        [
            tool_call("list_dir", {"path": "."}),
            tool_call("read_file", {"path": "README.md"}, "call-2"),
            tool_call("read_file", {"path": "settings.toml"}, "call-3"),
            tool_call(
                "write_file",
                {"path": "settings.toml", "content": 'app_name = "updated"\n'},
                "call-4",
            ),
            AIMessage(content="Updated the configured app name."),
        ]
    )

    result = run_agent("Update the app name as requested.", project, settings, llm=model)

    assert result.ok and result.tool_calls == 4
    assert (project / "settings.toml").read_text() == 'app_name = "updated"\n'


def test_agent_parses_json_tool_calls_from_ollama(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    model = ScriptedModel(
        [
            AIMessage(content='{"name":"list_dir","arguments":{"path":"."}}'),
            AIMessage(content="Inspected the project."),
        ]
    )

    result = run_agent(
        "Summarize this project.",
        project,
        settings,
        llm=model,
        allow_no_changes=True,
    )

    assert result.finished and result.tool_calls == 1


def test_agent_parses_ollama_api_function_tool_call_shape(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    model = ScriptedModel(
        [
            AIMessage(
                content=(
                    '{"tool_calls":[{"type":"function","function":'
                    '{"name":"list_dir","arguments":"{\\"path\\":\\".\\"}"}}]}'
                )
            ),
            AIMessage(content="Inspected the project."),
        ]
    )

    result = run_agent(
        "Summarize this project.",
        project,
        settings,
        llm=model,
        allow_no_changes=True,
    )

    assert result.finished and result.tool_calls == 1


def test_agent_parses_newline_delimited_ollama_tool_calls(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    model = ScriptedModel(
        [
            AIMessage(
                content=(
                    '{"name":"list_dir","arguments":{"path":"."}}\n'
                    '{"name":"write_file","arguments":{"path":"note.txt","content":"ok"}}'
                )
            ),
            AIMessage(content="Created note.txt."),
        ]
    )

    result = run_agent("Create a note.", project, settings, llm=model)

    assert result.finished and result.tool_calls == 2
    assert (project / "note.txt").read_text() == "ok"


def test_agent_parses_multiple_pretty_json_tool_calls(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    model = ScriptedModel(
        [
            tool_call("list_dir", {"path": "."}),
            AIMessage(
                content="""```json
{
  "name": "write_file",
  "arguments": {
    "path": "main.py",
    "content": "print(1)\\n"
  }
}
```
```json
{
  "name": "write_file",
  "arguments": {
    "path": "README.md",
    "content": "Demo\\n"
  }
}
```"""
            ),
            AIMessage(content="Created the project files."),
        ]
    )

    result = run_agent("Create two files.", project, settings, llm=model)

    assert result.finished and result.files_written == 2
    assert (project / "main.py").read_text() == "print(1)\n"
    assert (project / "README.md").read_text() == "Demo\n"


def test_agent_rejects_response_without_required_tool_call(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    model = ScriptedModel(
        [
            AIMessage(content="I will do that."),
            AIMessage(content="Let me inspect."),
            AIMessage(content="I inspected it."),
        ]
    )

    with pytest.raises(AgentError, match="tool inspeksi wajib"):
        run_agent("Change the project.", project, settings, llm=model)


def test_agent_retries_when_model_skips_initial_inspection(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    model = ScriptedModel(
        [
            AIMessage(content="I will inspect now."),
            tool_call("list_dir", {"path": "."}),
            AIMessage(content="Inspected the project."),
        ]
    )

    result = run_agent(
        "Summarize this project.",
        project,
        settings,
        llm=model,
        allow_no_changes=True,
    )

    assert result.finished and result.tool_calls == 1
    assert result.steps == 3


def test_agent_runs_verification_and_repairs_failures(monkeypatch, settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    responses = [
        tool_call("list_dir", {"path": "."}),
        tool_call("write_file", {"path": "main.py", "content": "print('initial')\n"}),
        AIMessage(content="I made a change."),
        tool_call("write_file", {"path": "main.py", "content": "print('fixed')\n"}),
        AIMessage(content="Fixed the failing check."),
    ]
    model = ScriptedModel(responses)
    outcomes = iter(
        [
            CommandResult(argv=["docker"], exit_code=1, output="assertion failed"),
            CommandResult(argv=["docker"], exit_code=0, output="1 passed"),
        ]
    )
    commands = []

    def fake_runner(command, sandbox, actual_settings, cwd=".", timeout_s=None):
        commands.append(command)
        return next(outcomes)

    monkeypatch.setattr("agent.agent.run_command_in_container", fake_runner)
    result = run_agent(
        "Fix the bug.",
        project,
        settings,
        verification_commands=["python -m pytest"],
        llm=model,
    )

    assert result.ok and result.verified
    assert result.steps == 5
    assert result.tool_calls == 3
    assert commands == ["python -m pytest", "python -m pytest"]
    assert len(result.verifications) == 1
    assert result.verifications[0].result.ok


def test_agent_stops_after_repair_limit(monkeypatch, settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    model = ScriptedModel(
        [
            tool_call("list_dir", {"path": "."}),
            tool_call("write_file", {"path": "main.py", "content": "print('attempt')\n"}),
            AIMessage(content="Attempted a fix."),
        ]
    )
    monkeypatch.setattr(
        "agent.agent.run_command_in_container",
        lambda *args, **kwargs: CommandResult(argv=["docker"], exit_code=1, output="failed"),
    )
    settings = replace(settings, max_fix_iterations=0)

    result = run_agent(
        "Fix the bug.",
        project,
        settings,
        verification_commands=["python -m pytest"],
        llm=model,
    )

    assert not result.ok
    assert result.finished is False
    assert not result.verifications[0].result.ok


def test_agent_does_not_report_success_when_no_file_was_written(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    model = ScriptedModel(
        [
            tool_call("list_dir", {"path": "."}),
            AIMessage(content="The project is already fine."),
            AIMessage(content="No changes are needed."),
            AIMessage(content="Completed."),
        ]
    )

    result = run_agent(
        "Change the source code.",
        project,
        settings,
        verification_commands=["python -m pytest"],
        llm=model,
    )

    assert not result.ok
    assert not result.finished
    assert result.files_written == 0
    assert result.verifications == ()


def test_agent_rejects_project_outside_workspace(settings, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(AgentError, match="WORKSPACE_DIR"):
        run_agent("Change something.", outside, settings, llm=ScriptedModel([]))


def test_agent_rejects_empty_instruction(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    with pytest.raises(AgentError, match="tidak boleh kosong"):
        run_agent("  ", project, settings, llm=ScriptedModel([]))


def test_agent_reports_step_limit(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    settings = replace(settings, max_agent_steps=1)
    model = ScriptedModel([tool_call("write_file", {"path": "a.txt", "content": "x"})])

    result = run_agent("Write a file.", project, settings, llm=model)

    assert not result.finished
    assert result.steps == 1


def test_agent_model_tool_budget_is_bounded(settings):
    project = settings.workspace_dir
    project.mkdir(parents=True)
    settings = replace(settings, max_agent_tool_calls=2)
    model = ScriptedModel(
        [
            tool_call("list_dir", {"path": "."}),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": "a", "content": "1"},
                        "id": "1",
                        "type": "tool_call",
                    },
                    {
                        "name": "write_file",
                        "args": {"path": "b", "content": "2"},
                        "id": "2",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="Stopped at the tool limit."),
        ]
    )

    result = run_agent("Write two files.", project, settings, llm=model)

    assert result.tool_calls == 2
    assert (project / "a").exists()
    assert not (project / "b").exists()
