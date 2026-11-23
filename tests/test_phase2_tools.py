import os
import sys

import pytest

from agent.schemas import CommandResult
from agent.tools import SandboxError
from agent.tools.container_runner import run_argv
from agent.tools.fs_tools import build_fs_tools


@pytest.fixture
def fs(sandbox):
    write_file, read_file, list_dir = build_fs_tools(sandbox, max_file_bytes=100)
    return write_file, read_file, list_dir


# ---------- Sandbox path ----------
def test_resolve_relatif_ok(sandbox):
    assert sandbox.resolve("a/b.txt") == sandbox.root / "a" / "b.txt"


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd", "..", "a/../.."])
def test_resolve_menolak_keluar_workspace(sandbox, bad):
    with pytest.raises(SandboxError):
        sandbox.resolve(bad)


def test_resolve_null_byte(sandbox):
    with pytest.raises(SandboxError):
        sandbox.resolve("a\x00b")


@pytest.mark.skipif(os.name == "nt", reason="symlink butuh hak khusus di Windows")
def test_symlink_escape_ditolak(sandbox, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (sandbox.root / "link").symlink_to(outside)
    with pytest.raises(SandboxError):
        sandbox.resolve("link/secret.txt")


def test_tulis_ke_node_modules_dilarang(sandbox):
    with pytest.raises(SandboxError):
        sandbox.resolve_for_write("app/node_modules/x.js")


# ---------- FS tools ----------
def test_write_lalu_read(fs):
    write_file, read_file, _ = fs
    assert write_file.invoke({"path": "app/src/page.tsx", "content": "export {}"}).startswith("OK")
    assert read_file.invoke({"path": "app/src/page.tsx"}) == "export {}"


def test_write_menolak_traversal_sebagai_teks_error(fs):
    write_file, *_ = fs
    out = write_file.invoke({"path": "../evil.txt", "content": "x"})
    assert out.startswith("ERROR")


def test_write_menolak_file_terlalu_besar(fs):
    write_file, *_ = fs
    assert "terlalu besar" in write_file.invoke({"path": "big.txt", "content": "x" * 101})


def test_write_ke_folder_ditolak(fs, sandbox):
    write_file, *_ = fs
    (sandbox.root / "d").mkdir()
    assert write_file.invoke({"path": "d", "content": "x"}).startswith("ERROR")


def test_read_tidak_ada(fs):
    _, read_file, _ = fs
    assert "tidak ditemukan" in read_file.invoke({"path": "nope.txt"})


def test_read_biner(fs, sandbox):
    _, read_file, _ = fs
    (sandbox.root / "b.bin").write_bytes(b"\xff\xfe\x00\x80")
    assert "bukan teks" in read_file.invoke({"path": "b.bin"})


def test_secret_files_are_not_read_or_written(fs, sandbox):
    write_file, read_file, list_dir = fs
    (sandbox.root / ".env").write_text("API_KEY=secret")
    assert read_file.invoke({"path": ".env"}).startswith("ERROR:")
    assert write_file.invoke({"path": ".env", "content": "API_KEY=changed"}).startswith("ERROR:")
    assert ".env" not in list_dir.invoke({"path": "."})
    assert read_file.invoke({"path": ".env.example"}).startswith("ERROR:")


def test_safe_environment_templates_remain_accessible(fs):
    write_file, read_file, _ = fs
    assert write_file.invoke({"path": ".env.example", "content": "API_KEY="}).startswith("OK:")
    assert read_file.invoke({"path": ".env.example"}) == "API_KEY="


def test_list_dir(fs, sandbox):
    write_file, _, list_dir = fs
    write_file.invoke({"path": "app/a.txt", "content": "1"})
    write_file.invoke({"path": "z.txt", "content": "1"})
    out = list_dir.invoke({"path": "."}).splitlines()
    assert out == ["app/", "z.txt"]  # folder lebih dulu


def test_list_dir_kosong_dan_traversal(fs):
    _, _, list_dir = fs
    assert list_dir.invoke({"path": "."}) == "(folder kosong)"
    assert list_dir.invoke({"path": ".."}).startswith("ERROR")


# ---------- Eksekusi ----------
def test_run_argv_sukses(tmp_path):
    r = run_argv([sys.executable, "-c", "print('halo')"], tmp_path, 10)
    assert r.ok and r.output == "halo"


def test_run_argv_exit_code_dan_stderr_digabung(tmp_path):
    code = "import sys; print('boom', file=sys.stderr); sys.exit(3)"
    r = run_argv([sys.executable, "-c", code], tmp_path, 10)
    assert r.exit_code == 3 and not r.ok and "boom" in r.output


def test_run_argv_timeout_mematikan_proses(tmp_path):
    r = run_argv([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 1)
    assert r.timed_out and not r.ok and r.duration_s < 10


def test_run_argv_output_dipotong_bagian_akhir_dipertahankan(tmp_path):
    code = "print('A' * 5000); print('ERROR_DI_AKHIR')"
    r = run_argv([sys.executable, "-c", code], tmp_path, 10, max_output_chars=200)
    assert r.truncated and "ERROR_DI_AKHIR" in r.output and len(r.output) < 400


def test_run_argv_stdin_ditutup_tidak_hang(tmp_path):
    r = run_argv([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], tmp_path, 5)
    assert r.ok and r.output == "''"


def test_run_argv_membuang_kode_warna_ansi(tmp_path):
    r = run_argv([sys.executable, "-c", "print('\\x1b[31mmerah\\x1b[0m')"], tmp_path, 5)
    assert r.output == "merah"


def test_run_argv_streams_output_to_callback(tmp_path):
    observed = []
    code = "import time; print('mulai', flush=True); time.sleep(.05); print('selesai')"

    result = run_argv(
        [sys.executable, "-c", code],
        tmp_path,
        5,
        on_output=observed.append,
    )

    assert result.ok
    assert result.output == "mulai\nselesai"
    assert "".join(observed) == "mulai\nselesai\n"


def test_run_argv_streaming_preserves_zero_output_limit(tmp_path):
    observed = []
    result = run_argv(
        [sys.executable, "-c", "print('output')"],
        tmp_path,
        5,
        max_output_chars=0,
        on_output=observed.append,
    )

    assert result.ok and result.truncated and result.output == ""
    assert "".join(observed) == "output\n"


def test_tool_run_command_uses_isolated_runner(monkeypatch, sandbox, settings):
    from agent.tools.shell_tools import build_shell_tools

    calls = []

    def fake_run(cmd, actual_sandbox, actual_settings, cwd=".", timeout_s=None):
        calls.append((cmd, actual_sandbox, actual_settings, cwd, timeout_s))
        return CommandResult(argv=["docker", "run"], exit_code=0, output="OK")

    monkeypatch.setattr("agent.tools.shell_tools.run_command_in_container", fake_run)
    (run_command,) = build_shell_tools(sandbox, settings)
    assert "exit_code=0" in run_command.invoke({"cmd": "python -m pytest"})
    assert calls[0][0] == "python -m pytest"
    assert calls[0][1] is sandbox
    assert calls[0][2] is settings


def test_tool_run_command_surfaces_rejected_paths(sandbox, settings):
    from agent.tools.shell_tools import build_shell_tools

    (run_command,) = build_shell_tools(sandbox, settings)
    assert run_command.invoke({"cmd": "python -m pytest", "cwd": ".."}).startswith("ERROR:")
