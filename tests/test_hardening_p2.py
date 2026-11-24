"""Regresi untuk hardening P2: TOCTOU, symlink, ukuran baca, dan container."""

import os
from dataclasses import replace

import pytest

from agent.config import SettingsError
from agent.tools import Sandbox, SandboxError
from agent.tools.container_runner import _docker_command, _external_symlinks
from agent.tools.fs_tools import build_fs_tools
from agent.tools.sandbox import is_sensitive_relative_path

needs_symlink = pytest.mark.skipif(os.name == "nt", reason="symlink butuh hak khusus di Windows")


# ---------- Sandbox: TOCTOU ----------
@needs_symlink
def test_write_bytes_menolak_symlink_di_target(sandbox, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("rahasia")
    link = sandbox.root / "link.txt"
    link.symlink_to(outside)

    with pytest.raises(SandboxError):
        sandbox.write_bytes(link, b"ditimpa")

    assert outside.read_text() == "rahasia"


def test_write_bytes_menolak_perubahan_inode_saat_penulisan(monkeypatch, sandbox):
    target = sandbox.root / "a.txt"
    target.write_text("awal")
    original_lstat = os.lstat
    real_info = original_lstat(target)

    class FakeStat:
        st_dev = real_info.st_dev + 1
        st_ino = real_info.st_ino + 1
        st_mode = real_info.st_mode

    def fake_lstat(path):
        if str(path) == str(target):
            return FakeStat()
        return original_lstat(path)

    monkeypatch.setattr("agent.tools.sandbox.os.lstat", fake_lstat)
    with pytest.raises(SandboxError, match="diganti saat penulisan"):
        sandbox.write_bytes(target, b"x")


def test_write_bytes_menolak_folder(sandbox):
    (sandbox.root / "folder").mkdir()
    with pytest.raises(SandboxError, match="folder"):
        sandbox.write_bytes("folder", b"x")


def test_write_bytes_menolak_path_keluar_workspace(sandbox):
    with pytest.raises(SandboxError):
        sandbox.write_bytes("../luar.txt", b"x")


def test_write_bytes_menolak_ruang_disk_tipis(monkeypatch, sandbox):
    monkeypatch.setattr("agent.tools.sandbox.shutil_disk_free_mb", lambda _: 10.0)
    with pytest.raises(SandboxError, match="ruang disk tidak cukup"):
        sandbox.write_bytes("a.txt", b"x", min_free_disk_mb=256)


def test_write_bytes_menerima_penulisan_normal(sandbox):
    sandbox.write_bytes("app/a.txt", b"halo")
    assert (sandbox.root / "app" / "a.txt").read_bytes() == b"halo"


# ---------- Pola sensitif ----------
@pytest.mark.parametrize(
    "name",
    [
        "secrets.yaml",
        "secrets.json",
        "credentials.yaml",
        "id_rsa.pub",
        "authorized_keys",
        ".npm_token",
        "server.secret",
        "config/.ssh/id_ed25519",
    ],
)
def test_pola_sensitif_baru_dikenali(name):
    assert is_sensitive_relative_path(name)


@pytest.mark.parametrize("name", ["main.py", "README.md", ".env.example", "secrets.py"])
def test_file_proyek_biasa_tidak_terkena(name):
    assert not is_sensitive_relative_path(name)


# ---------- FS tools ----------
def test_read_file_menolak_file_besar_tanpa_rentang(sandbox):
    _, read_file, _ = build_fs_tools(sandbox, max_file_bytes=100, max_read_chars=50)
    (sandbox.root / "big.txt").write_bytes(b"x" * 200)
    assert "terlalu besar" in read_file.invoke({"path": "big.txt"})


def test_read_file_rentang_baca_file_besar_tanpa_muat_penuh(sandbox):
    _, read_file, _ = build_fs_tools(sandbox, max_file_bytes=100, max_read_chars=500)
    (sandbox.root / "big.txt").write_text("baris\n" * 40)
    out = read_file.invoke({"path": "big.txt", "start_line": 2, "end_line": 3})
    assert out == "2: baris\n3: baris"


@needs_symlink
def test_write_file_menolak_symlink_keluar_workspace(sandbox, tmp_path):
    write_file, *_ = build_fs_tools(sandbox)
    outside = tmp_path / "rahasia.txt"
    outside.write_text("jangan diubah")
    (sandbox.root / "link.txt").symlink_to(outside)

    out = write_file.invoke({"path": "link.txt", "content": "ditimpa"})
    assert out.startswith("ERROR")
    assert outside.read_text() == "jangan diubah"


# ---------- Container ----------
@needs_symlink
def test_external_symlinks_ditemukan(sandbox, tmp_path):
    outside = tmp_path / "luar"
    outside.mkdir()
    (sandbox.root / "link").symlink_to(outside)
    assert _external_symlinks(sandbox.root) == ["link"]


@needs_symlink
def test_docker_command_menolak_symlink_keluar_workspace(monkeypatch, settings):
    monkeypatch.setattr("agent.tools.container_runner.shutil.which", lambda _: "/usr/bin/docker")
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "pyproject.toml").write_text("[project]\nname='x'\n")
    outside = settings.workspace_dir.parent / "luar"
    outside.mkdir()
    (settings.workspace_dir / "link").symlink_to(outside)

    with pytest.raises(SandboxError, match="symlink ke luar workspace"):
        _docker_command("true", Sandbox(settings.workspace_dir), settings, ".")


def test_docker_command_memakai_noexec_tmpfs(monkeypatch, settings):
    monkeypatch.setattr("agent.tools.container_runner.shutil.which", lambda _: "/usr/bin/docker")
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "package.json").write_text("{}")
    argv, _ = _docker_command("true", Sandbox(settings.workspace_dir), settings, ".")
    tmpfs = argv[argv.index("--tmpfs") + 1]
    assert "noexec" in tmpfs and "nodev" in tmpfs and "nosuid" in tmpfs


def test_docker_command_kuota_opsional(monkeypatch, settings):
    monkeypatch.setattr("agent.tools.container_runner.shutil.which", lambda _: "/usr/bin/docker")
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "package.json").write_text("{}")
    plain, _ = _docker_command("true", Sandbox(settings.workspace_dir), settings, ".")
    assert "--storage-opt" not in plain

    quota = replace(settings, sandbox_disk_quota_mb=2048)
    limited, _ = _docker_command("true", Sandbox(settings.workspace_dir), quota, ".")
    assert limited[limited.index("--storage-opt") + 1] == "size=2048m"


def test_settings_disk_defaults_dan_validasi(settings):
    assert settings.sandbox_min_free_disk_mb >= 0
    with pytest.raises(SettingsError, match="MIN_FREE_DISK"):
        replace(settings, sandbox_min_free_disk_mb=-1)
    with pytest.raises(SettingsError, match="DISK_QUOTA"):
        replace(settings, sandbox_disk_quota_mb=-1)
