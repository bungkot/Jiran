import json

import pytest

from agent.tools.container_runner import (
    _docker_command,
    _secret_mount_args,
    detect_sandbox_image,
)
from agent.tools.sandbox import Sandbox, SandboxError


def test_detect_node_project_image(settings):
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "package.json").write_text(json.dumps({"name": "sample"}))
    assert detect_sandbox_image(settings.workspace_dir) == "node:22-bookworm-slim"


def test_detect_python_project_image(settings):
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "pyproject.toml").write_text("[project]\nname='sample'\n")
    assert detect_sandbox_image(settings.workspace_dir) == "python:3.13-slim"


def test_detect_dotnet_project_image(settings):
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "service.csproj").write_text("<Project />")
    assert detect_sandbox_image(settings.workspace_dir) == "mcr.microsoft.com/dotnet/sdk:10.0"


def test_detect_composer_project_image(settings):
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "composer.json").write_text("{}")
    assert detect_sandbox_image(settings.workspace_dir) == "composer:2"


def test_custom_image_supports_unrecognized_stack(settings):
    settings.workspace_dir.mkdir(parents=True)
    assert detect_sandbox_image(settings.workspace_dir, "registry.example/toolchain:1") == (
        "registry.example/toolchain:1"
    )


def test_detect_python_fallback_from_extension(settings):
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "hello.py").write_text("print('hi')\n")
    assert detect_sandbox_image(settings.workspace_dir) == "python:3.13-slim"


def test_unknown_stack_requires_explicit_image(settings):
    settings.workspace_dir.mkdir(parents=True)
    with pytest.raises(SandboxError, match="AGENT_SANDBOX_IMAGE"):
        detect_sandbox_image(settings.workspace_dir)


def test_docker_command_is_restricted_and_mounts_only_project(monkeypatch, settings):
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "go.mod").write_text("module sample\n")
    monkeypatch.setattr("agent.tools.container_runner.shutil.which", lambda name: "/usr/bin/docker")
    sandbox = Sandbox(settings.workspace_dir)

    argv, name = _docker_command("go test ./...", sandbox, settings, ".")

    assert argv[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert any(arg.startswith("type=bind,source=") for arg in argv)
    assert "NEXT_TELEMETRY_DISABLED=1" in argv
    assert not any(arg.startswith("DOCKER_HOST=") for arg in argv)
    assert "go test ./..." in argv
    assert name.startswith("coding-agent-")


def test_docker_command_rejects_empty_or_oversized_commands(monkeypatch, settings):
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "package.json").write_text("{}")
    monkeypatch.setattr("agent.tools.container_runner.shutil.which", lambda name: "/usr/bin/docker")
    sandbox = Sandbox(settings.workspace_dir)

    with pytest.raises(SandboxError, match="kosong"):
        _docker_command(" ", sandbox, settings, ".")
    with pytest.raises(SandboxError, match="4000"):
        _docker_command("x" * 4001, sandbox, settings, ".")


def test_docker_command_rejects_workspace_escape(monkeypatch, settings):
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "package.json").write_text("{}")
    monkeypatch.setattr("agent.tools.container_runner.shutil.which", lambda name: "/usr/bin/docker")

    with pytest.raises(SandboxError):
        _docker_command("npm test", Sandbox(settings.workspace_dir), settings, "..")


def test_secret_paths_are_masked_from_command_containers(settings):
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / ".env").write_text("API_TOKEN=secret")
    (settings.workspace_dir / ".env.example").write_text("API_TOKEN=")
    (settings.workspace_dir / ".ssh").mkdir()
    (settings.workspace_dir / ".ssh" / "id_ed25519").write_text("private")
    (settings.workspace_dir / ".git").mkdir()

    args = _secret_mount_args(settings.workspace_dir)

    assert "type=bind,source=/dev/null,target=/workspace/.env,readonly" in args
    assert not any(".env.example" in arg for arg in args)
    assert "/workspace/.ssh:rw,nosuid,size=1m" in args
    assert "/workspace/.git:rw,nosuid,size=1m" in args
