import pytest

from agent.tools.edit_tools import build_edit_tools
from agent.tools.fs_tools import build_fs_tools


@pytest.fixture
def tools(sandbox):
    write_file, read_file, _ = build_fs_tools(sandbox)
    edit_file, search_files = build_edit_tools(sandbox, max_file_bytes=200)
    return write_file, read_file, edit_file, search_files


def test_edit_file_mengganti_teks_unik(tools):
    write_file, read_file, edit_file, _ = tools
    write_file.invoke({"path": "a.py", "content": "x = 1\ny = 2\n"})
    result = edit_file.invoke({"path": "a.py", "old_str": "y = 2", "new_str": "y = 3"})
    assert result.startswith("OK: 1")
    assert read_file.invoke({"path": "a.py"}) == "x = 1\ny = 3\n"


def test_edit_file_menolak_cocokan_ambigu_kecuali_replace_all(tools):
    write_file, read_file, edit_file, _ = tools
    write_file.invoke({"path": "a.py", "content": "x = 1\nx = 1\n"})
    ambiguous = edit_file.invoke({"path": "a.py", "old_str": "x = 1", "new_str": "x = 2"})
    assert ambiguous.startswith("ERROR") and "2 kali" in ambiguous
    result = edit_file.invoke(
        {"path": "a.py", "old_str": "x = 1", "new_str": "x = 2", "replace_all": True}
    )
    assert result.startswith("OK: 2")
    assert read_file.invoke({"path": "a.py"}) == "x = 2\nx = 2\n"


@pytest.mark.parametrize(
    "args",
    [
        {"path": "a.py", "old_str": "tidak-ada", "new_str": "x"},
        {"path": "baru.py", "old_str": "a", "new_str": "b"},
        {"path": "../luar.py", "old_str": "a", "new_str": "b"},
        {"path": "node_modules/a.js", "old_str": "a", "new_str": "b"},
        {"path": "a.py", "old_str": "", "new_str": "b"},
        {"path": "a.py", "old_str": "x = 1", "new_str": "x = 1"},
    ],
)
def test_edit_file_kasus_ditolak(tools, args):
    write_file, _, edit_file, _ = tools
    write_file.invoke({"path": "a.py", "content": "x = 1\n"})
    assert edit_file.invoke(args).startswith("ERROR")


def test_edit_file_menolak_file_sensitif_dan_hasil_terlalu_besar(tools, sandbox):
    write_file, _, edit_file, _ = tools
    (sandbox.root / ".env").write_text("SECRET=1")
    assert edit_file.invoke({"path": ".env", "old_str": "SECRET", "new_str": "X"}).startswith(
        "ERROR"
    )
    write_file.invoke({"path": "a.py", "content": "x = 1\n"})
    big = edit_file.invoke({"path": "a.py", "old_str": "x = 1", "new_str": "y" * 500})
    assert big.startswith("ERROR") and "terlalu besar" in big


def test_search_files_substring_regex_glob(tools, sandbox):
    write_file, _, _, search_files = tools
    write_file.invoke({"path": "pkg/m.py", "content": "def hello():\n    return 'Hello'\n"})
    write_file.invoke({"path": "notes.txt", "content": "hello"})
    out = search_files.invoke({"pattern": "hello"})
    assert "pkg/m.py:1: def hello():" in out and "pkg/m.py:2:" in out
    assert "notes.txt" not in search_files.invoke({"pattern": "hello", "glob": "*.py"})
    assert "pkg/m.py:1" in search_files.invoke({"pattern": r"def \w+\(", "regex": True})
    assert search_files.invoke({"pattern": "(", "regex": True}).startswith("ERROR: regex")
    assert search_files.invoke({"pattern": "tidak-ada-ini"}) == "(tidak ada hasil)"


def test_search_files_melewati_dependency_dan_secret(tools, sandbox):
    (sandbox.root / "node_modules").mkdir()
    (sandbox.root / "node_modules" / "lib.js").write_text("needle")
    (sandbox.root / ".env").write_text("needle")
    _, _, _, search_files = tools
    assert search_files.invoke({"pattern": "needle"}) == "(tidak ada hasil)"
    assert search_files.invoke({"pattern": "x", "path": "../.."}).startswith("ERROR")


def test_search_files_membatasi_jumlah_hasil(tools, sandbox):
    for index in range(150):
        (sandbox.root / f"f{index}.txt").write_text("needle")
    _, _, _, search_files = tools
    assert "dibatasi" in search_files.invoke({"pattern": "needle"})


def test_read_file_rentang_baris(tools):
    write_file, read_file, _, _ = tools
    write_file.invoke({"path": "a.txt", "content": "satu\ndua\ntiga\n"})
    assert read_file.invoke({"path": "a.txt", "start_line": 2, "end_line": 3}) == "2: dua\n3: tiga"
    assert read_file.invoke({"path": "a.txt", "start_line": 99}).startswith("ERROR")
    assert read_file.invoke({"path": "a.txt"}) == "satu\ndua\ntiga\n"
