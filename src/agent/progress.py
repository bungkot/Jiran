"""Safe, concise progress messages for interactive clients."""

from __future__ import annotations

import shlex
from typing import Any


def _command_name(command: Any) -> str:
    if not isinstance(command, str) or not command.strip():
        return "command"
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else "command"


def summarize_event(event: dict[str, Any]) -> str | None:
    name = event.get("event")
    step = event.get("step")
    if name == "run.started":
        return "Agent dimulai"
    if name == "goal.started":
        project = event.get("project", "?")
        limit = event.get("max_iterations", "?")
        return f"Goal dimulai — proyek {project}, batas {limit} putaran"
    if name == "goal.plan.started":
        return "Menganalisa goal dan menyusun tugas"
    if name == "goal.plan.created":
        todos = event.get("todos")
        count = len(todos) if isinstance(todos, list) else event.get("count", "?")
        return f"Rencana dibuat — {count} tugas kecil"
    if name == "goal.plan.fallback":
        return "Rencana gagal; goal dikerjakan sebagai satu tugas"
    if name == "goal.todo.started":
        index = event.get("iteration", "?")
        total = event.get("total_todos", "?")
        return f"Tugas {index}/{total} dimulai"
    if name == "goal.todo.completed":
        index = event.get("iteration", "?")
        state = "selesai" if event.get("complete") else "belum selesai"
        return f"Tugas {index} {state}"
    if name == "goal.iteration.started":
        return f"Putaran {event.get('iteration', '?')} dimulai"
    if name == "goal.iteration.completed":
        state = "tercapai" if event.get("complete") else "belum tercapai"
        return f"Putaran {event.get('iteration', '?')} selesai — {state}"
    if name == "goal.iteration.failed":
        return f"Putaran {event.get('iteration', '?')} gagal"
    if name == "model.request":
        return f"Menunggu model (langkah {step or '?'}, percobaan {event.get('attempt', 1)})"
    if name == "model.response":
        calls = event.get("tool_calls")
        if isinstance(calls, list) and calls:
            names = ", ".join(
                str(call.get("name", "tool")) for call in calls if isinstance(call, dict)
            )
            return f"Model meminta tool: {names or 'tool'}"
        return f"Respons model diterima (langkah {step or '?'})"
    if name in {"model.error", "judge.error"}:
        owner = "Model" if name == "model.error" else "Judge"
        return f"{owner} gagal: {event.get('error_type', 'error')}"
    if name == "model.retry":
        return f"Mencoba ulang permintaan model (percobaan {event.get('attempt', '?')})"
    if name == "model.empty_response":
        return "Respons model kosong; meminta ringkasan sekali lagi"
    if name == "tool.started":
        tool = event.get("tool", "tool")
        args = event.get("arguments")
        if tool == "run_command" and isinstance(args, dict):
            return f"Menjalankan {_command_name(args.get('cmd'))}"
        if isinstance(args, dict) and isinstance(args.get("path"), str):
            return f"Menjalankan {tool}: {args['path']}"
        return f"Menjalankan tool: {tool}"
    if name == "tool.completed":
        state = "selesai" if event.get("ok") else "gagal"
        return f"Tool {event.get('tool', 'tool')} {state}"
    if name == "verification.started":
        return f"Memverifikasi dengan {_command_name(event.get('command'))}"
    if name in {"verification.completed", "goal.verification.completed"}:
        state = "LULUS" if event.get("ok") else "GAGAL"
        return f"Verifikasi {_command_name(event.get('command'))}: {state}"
    if name == "judge.request":
        return "Judge sedang menilai hasil"
    if name == "judge.response":
        return "Penilaian judge diterima"
    if name == "goal.acceptance.checked":
        state = (
            "terpenuhi"
            if (
                event.get("verification_ok")
                and not event.get("missing_files")
                and event.get("judge_ok")
            )
            else "belum terpenuhi"
        )
        skipped = " (tanpa judge)" if event.get("judge_skipped") else ""
        return f"Kriteria penerimaan {state}{skipped}"
    if name == "goal.completed":
        return f"Goal tercapai dalam {event.get('iterations', '?')} putaran"
    if name == "goal.failed":
        return f"Goal belum tercapai ({event.get('reason', 'gagal')})"
    if name == "run.completed":
        return f"Agent selesai ({event.get('status', 'selesai')})"
    return None
