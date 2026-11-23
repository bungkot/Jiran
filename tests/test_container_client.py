import json
from dataclasses import replace

import pytest

from agent.config import SettingsError
from agent.container_client import (
    ContainerError,
    ContainerGoalRequest,
    build_goal_container_argv,
    container_ollama_url,
    run_container_goal,
)
from agent.schemas import CommandResult


def _argv(request, settings, *, workspace, logs, name="coding-agent-goal-test"):
    return build_goal_container_argv(
        request,
        settings,
        docker="docker",
        workspace=workspace,
        logs=logs,
        container_name=name,
    )


def test_container_ollama_url_rewrites_only_local_hosts_on_bridge():
    assert (
        container_ollama_url("http://localhost:11434", "bridge")
        == "http://host.docker.internal:11434"
    )
    assert (
        container_ollama_url("http://127.0.0.1:1234/api", "bridge")
        == "http://host.docker.internal:1234/api"
    )
    assert container_ollama_url("http://ollama.lan:11434", "bridge") == "http://ollama.lan:11434"
    assert container_ollama_url("http://localhost:11434", "host") == "http://localhost:11434"


def test_build_goal_container_argv_uses_same_paths_and_goal_flags(settings):
    workspace = settings.workspace_dir.resolve()
    logs = settings.logs_dir.resolve()
    request = ContainerGoalRequest(
        goal="Buat kalkulator CLI",
        project="kalkulator",
        verification_commands=("python -m pytest",),
        required_files=("main.py",),
        max_iterations=3,
        allow_no_changes=True,
    )
    argv = _argv(request, settings, workspace=workspace, logs=logs)

    assert argv[:3] == ["docker", "run", "--rm"]
    assert "coding-agent-goal-test" in argv
    assert argv[argv.index("--network") + 1] == "host"
    assert "host.docker.internal:host-gateway" not in argv
    assert "OLLAMA_BASE_URL=http://localhost:11434" in argv
    assert f"WORKSPACE_DIR={workspace}" in argv
    assert f"AGENT_LOGS_DIR={logs}" in argv
    assert f"type=bind,source={workspace},target={workspace}" in argv
    assert f"type=bind,source={logs},target={logs}" in argv
    assert "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock" in argv
    assert "--user" in argv
    assert argv[-13:] == [
        "goal",
        "--json",
        "--project",
        "kalkulator",
        "--verify",
        "python -m pytest",
        "--require-file",
        "main.py",
        "--max-iterations",
        "3",
        "--allow-no-change",
        "--",
        "Buat kalkulator CLI",
    ]


def test_build_goal_container_argv_bridge_mode_uses_host_gateway(settings):
    settings = replace(settings, container_network="bridge")
    workspace = settings.workspace_dir.resolve()
    argv = _argv(
        ContainerGoalRequest(goal="ringkas"),
        settings,
        workspace=workspace,
        logs=settings.logs_dir.resolve(),
    )
    assert argv[argv.index("--network") + 1] == "bridge"
    assert "host.docker.internal:host-gateway" in argv
    assert "OLLAMA_BASE_URL=http://host.docker.internal:11434" in argv


def test_build_goal_container_argv_can_skip_docker_socket(settings):
    settings = replace(settings, container_docker_socket=False)
    workspace = settings.workspace_dir.resolve()
    argv = _argv(
        ContainerGoalRequest(goal="ringkas"),
        settings,
        workspace=workspace,
        logs=settings.logs_dir.resolve(),
    )
    assert not any("docker.sock" in part for part in argv)
    assert "--group-add" not in argv


def test_run_container_goal_parses_json_payload(monkeypatch, settings):
    payload = {
        "achieved": True,
        "reason": "Tujuan tercapai.",
        "project": "demo",
        "iterations": 2,
        "files_written": 3,
        "response": "Selesai.",
    }
    seen = {}

    def fake_run_argv(argv, cwd, timeout_s, max_output_chars):
        seen["argv"] = argv
        seen["timeout"] = timeout_s
        return CommandResult(argv=argv, exit_code=0, output=f"progress\n{json.dumps(payload)}\n")

    monkeypatch.setattr("agent.container_client.shutil.which", lambda _: "docker")
    monkeypatch.setattr("agent.container_client.run_argv", fake_run_argv)

    result = run_container_goal(
        ContainerGoalRequest(goal="Buat demo", project="demo"),
        settings,
        timeout_s=30,
    )

    assert result.ok and result.achieved
    assert result.payload == payload
    assert result.exit_code == 0
    assert seen["timeout"] == 30
    assert seen["argv"][-2:] == ["--", "Buat demo"]


def test_run_container_goal_stream_hides_internal_json_payload(monkeypatch, settings):
    payload = {"achieved": True, "reason": "selesai"}
    streamed = []

    def fake_run_argv(argv, cwd, timeout_s, max_output_chars, on_output=None):
        output = f"[agent] Goal tercapai\n{json.dumps(payload)}\n"
        for line in output.splitlines(keepends=True):
            assert on_output is not None
            on_output(line)
        return CommandResult(argv=argv, exit_code=0, output=output)

    monkeypatch.setattr("agent.container_client.shutil.which", lambda _: "docker")
    result = run_container_goal(
        ContainerGoalRequest(goal="Buat demo"),
        settings,
        runner=fake_run_argv,
        on_output=streamed.append,
    )

    assert result.ok
    assert streamed == ["[agent] Goal tercapai\n"]


def test_run_container_goal_cleans_up_on_timeout(monkeypatch, settings):
    removed = []

    def fake_run_argv(argv, cwd, timeout_s, max_output_chars):
        return CommandResult(argv=argv, exit_code=None, timed_out=True, output=" timeout ")

    monkeypatch.setattr("agent.container_client.shutil.which", lambda _: "docker")
    monkeypatch.setattr("agent.container_client.run_argv", fake_run_argv)
    monkeypatch.setattr(
        "agent.container_client._force_remove_container",
        lambda docker, name: removed.append(name),
    )

    result = run_container_goal(ContainerGoalRequest(goal="Buat demo"), settings)

    assert not result.ok and result.timed_out
    assert len(removed) == 1
    assert removed[0].startswith("coding-agent-goal-")


def test_run_container_goal_requires_docker_cli(monkeypatch, settings):
    monkeypatch.setattr("agent.container_client.shutil.which", lambda _: None)
    with pytest.raises(ContainerError, match="Docker CLI"):
        run_container_goal(ContainerGoalRequest(goal="Buat demo"), settings)


def test_invalid_container_settings_are_rejected(settings):
    with pytest.raises(SettingsError, match="AGENT_CONTAINER_IMAGE"):
        replace(settings, container_image="")
    with pytest.raises(SettingsError, match="CONTAINER_GOAL_TIMEOUT_S"):
        replace(settings, container_goal_timeout_s=0)
    with pytest.raises(SettingsError, match="AGENT_CONTAINER_NETWORK"):
        replace(settings, container_network="none")
