import pytest

from agent.progress import _command_name, summarize_event


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (None, "command"),
        ("", "command"),
        ("   ", "command"),
        (123, "command"),
        ("python -m pytest -q", "python"),
        ('"python -m pytest" -q', "python -m pytest"),
        ("'python -m pytest -q", "'python"),  # kutipan rusak -> fallback split()
    ],
)
def test_command_name_mengambil_nama_perintah_saja(command, expected):
    assert _command_name(command) == expected


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"event": "run.started"}, "Agent dimulai"),
        (
            {"event": "goal.started", "project": "demo", "max_iterations": 3},
            "Goal dimulai — proyek demo, batas 3 putaran",
        ),
        ({"event": "goal.started"}, "Goal dimulai — proyek ?, batas ? putaran"),
        ({"event": "goal.plan.started"}, "Menganalisa goal dan menyusun tugas"),
        ({"event": "goal.plan.created", "todos": ["a", "b"]}, "Rencana dibuat — 2 tugas kecil"),
        ({"event": "goal.plan.created", "count": 5}, "Rencana dibuat — 5 tugas kecil"),
        (
            {"event": "goal.plan.fallback"},
            "Rencana gagal; goal dikerjakan sebagai satu tugas",
        ),
        ({"event": "goal.todo.started", "iteration": 1, "total_todos": 3}, "Tugas 1/3 dimulai"),
        ({"event": "goal.todo.completed", "iteration": 2, "complete": True}, "Tugas 2 selesai"),
        ({"event": "goal.todo.completed", "iteration": 2}, "Tugas 2 belum selesai"),
        ({"event": "goal.iteration.started", "iteration": 1}, "Putaran 1 dimulai"),
        (
            {"event": "goal.iteration.completed", "iteration": 1, "complete": True},
            "Putaran 1 selesai — tercapai",
        ),
        (
            {"event": "goal.iteration.completed", "iteration": 1},
            "Putaran 1 selesai — belum tercapai",
        ),
        ({"event": "goal.iteration.failed", "iteration": 4}, "Putaran 4 gagal"),
        (
            {"event": "model.request", "step": 2, "attempt": 3},
            "Menunggu model (langkah 2, percobaan 3)",
        ),
        ({"event": "model.error", "error_type": "TimeoutError"}, "Model gagal: TimeoutError"),
        ({"event": "judge.error"}, "Judge gagal: error"),
        ({"event": "model.retry", "attempt": 2}, "Mencoba ulang permintaan model (percobaan 2)"),
        (
            {"event": "model.empty_response"},
            "Respons model kosong; meminta ringkasan sekali lagi",
        ),
        ({"event": "tool.completed", "tool": "write_file", "ok": True}, "Tool write_file selesai"),
        ({"event": "tool.completed", "tool": "write_file"}, "Tool write_file gagal"),
        (
            {"event": "verification.started", "command": "python -m unittest -q -v"},
            "Memverifikasi dengan python",
        ),
        (
            {"event": "verification.completed", "command": "pytest -q", "ok": True},
            "Verifikasi pytest: LULUS",
        ),
        (
            {"event": "goal.verification.completed", "command": "pytest -q"},
            "Verifikasi pytest: GAGAL",
        ),
        ({"event": "judge.request"}, "Judge sedang menilai hasil"),
        ({"event": "judge.response"}, "Penilaian judge diterima"),
        (
            {
                "event": "goal.acceptance.checked",
                "verification_ok": True,
                "judge_ok": True,
            },
            "Kriteria penerimaan terpenuhi",
        ),
        (
            {
                "event": "goal.acceptance.checked",
                "verification_ok": True,
                "missing_files": ["main.py"],
                "judge_ok": True,
            },
            "Kriteria penerimaan belum terpenuhi",
        ),
        (
            {"event": "goal.acceptance.checked", "verification_ok": True},
            "Kriteria penerimaan belum terpenuhi",
        ),
        ({"event": "goal.completed", "iterations": 2}, "Goal tercapai dalam 2 putaran"),
        (
            {"event": "goal.failed", "reason": "iteration_limit"},
            "Goal belum tercapai (iteration_limit)",
        ),
        ({"event": "goal.failed"}, "Goal belum tercapai (gagal)"),
        ({"event": "run.completed", "status": "verified"}, "Agent selesai (verified)"),
        ({"event": "run.completed"}, "Agent selesai (selesai)"),
        ({"event": "tidak_dikenal"}, None),
    ],
)
def test_summarize_event_pesan_aman(event, expected):
    assert summarize_event(event) == expected


def test_summarize_event_model_response_tanpa_tool():
    assert summarize_event({"event": "model.response", "step": 3}) == (
        "Respons model diterima (langkah 3)"
    )
    assert summarize_event({"event": "model.response"}) == ("Respons model diterima (langkah ?)")


def test_summarize_event_model_response_dengan_tool():
    event = {
        "event": "model.response",
        "tool_calls": [{"name": "read_file"}, {"name": "write_file"}, "bukan-dict"],
    }
    assert summarize_event(event) == "Model meminta tool: read_file, write_file"

    event = {"event": "model.response", "tool_calls": ["bukan-dict"]}
    assert summarize_event(event) == "Model meminta tool: tool"


def test_summarize_event_tool_started_tanpa_membocorkan_argumen():
    event = {
        "event": "tool.started",
        "tool": "run_command",
        "arguments": {"cmd": "python -c 'print(rahasia)'"},
    }
    assert summarize_event(event) == "Menjalankan python"

    event = {"event": "tool.started", "tool": "write_file", "arguments": {"path": "main.py"}}
    assert summarize_event(event) == "Menjalankan write_file: main.py"

    event = {"event": "tool.started", "tool": "write_file", "arguments": {"path": 7}}
    assert summarize_event(event) == "Menjalankan tool: write_file"

    assert summarize_event({"event": "tool.started"}) == "Menjalankan tool: tool"


def test_summarize_event_verification_tanpa_perintah():
    assert summarize_event({"event": "verification.started"}) == "Memverifikasi dengan command"
