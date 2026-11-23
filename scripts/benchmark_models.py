#!/usr/bin/env python3
"""Benchmark model Ollama pada agent ini, dengan penilaian mesin (--verify).

Pemakaian (dari folder proyek, Ollama dan Docker aktif):

    python scripts/benchmark_models.py qwen3.5:4b gemma4:e4b qwen2.5-coder:7b

Setiap model menjalankan 3 tugas kecil lewat `coding-agent goal` (mode lokal).
Hasil dicetak sebagai tabel dan disimpan ke logs/benchmark-<waktu>.json.
Model yang belum ada akan di-pull otomatis. Tidak ada data yang dikirim ke luar.
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = [
    {
        "name": "fungsi+tes",
        "goal": (
            "Buat modul slugify.py berisi fungsi slugify(text) yang mengubah teks menjadi slug "
            "huruf kecil dipisah tanda minus, dan test_slugify.py dengan pytest untuk 3 kasus."
        ),
        "verify": "python -m py_compile slugify.py test_slugify.py",
        "files": ["slugify.py", "test_slugify.py"],
    },
    {
        "name": "cli-kecil",
        "goal": (
            "Buat cli.py dengan argparse: perintah `count FILE` mencetak jumlah baris, kata, "
            "dan karakter file teks seperti wc."
        ),
        "verify": "python -m py_compile cli.py && python cli.py --help",
        "files": ["cli.py"],
    },
    {
        "name": "multi-file",
        "goal": (
            "Buat paket kecil 'inventory' dengan inventory/__init__.py, inventory/models.py "
            "(dataclass Item name,qty,price) dan inventory/store.py (kelas Store: add, total_value)."
        ),
        "verify": (
            "python -c \"from inventory.store import Store; from inventory.models import Item; "
            "s=Store(); s.add(Item('a',2,1.5)); assert s.total_value()==3.0\""
        ),
        "files": ["inventory/store.py", "inventory/models.py"],
    },
]


def run(cmd: list[str], timeout: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env, check=False
    )


def bench_task(model: str, task: dict, timeout: int) -> dict:
    project = f"bench-{model.replace(':', '-').replace('/', '-')}-{task['name']}-{int(time.time())}"
    cmd = [sys.executable, "-m", "agent.main", "goal", "--json", "--project", project,
           "--max-iterations", "3", "--verify", task["verify"]]
    for path in task["files"]:
        cmd += ["--require-file", path]
    cmd += ["--", task["goal"]]
    env = {**os.environ, "OLLAMA_MODEL": model, "PYTHONPATH": str(ROOT / "src")}
    start = time.perf_counter()
    try:
        proc = run(cmd, timeout, env)
        elapsed = time.perf_counter() - start
        payload = {}
        for line in reversed(proc.stdout.splitlines()):
            if line.strip().startswith("{"):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        return {
            "task": task["name"], "ok": bool(payload.get("achieved")), "seconds": round(elapsed, 1),
            "iterations": payload.get("iterations"), "tool_calls": payload.get("tool_calls"),
            "note": (payload.get("reason") or payload.get("error") or proc.stderr[-200:]).strip(),
        }
    except subprocess.TimeoutExpired:
        return {"task": task["name"], "ok": False, "seconds": float(timeout), "iterations": None,
                "tool_calls": None, "note": f"timeout {timeout}s"}


def main(models: list[str]) -> int:
    if not models:
        print(__doc__)
        return 1
    timeout = int(os.environ.get("BENCH_TASK_TIMEOUT_S", "1800"))
    results: dict[str, list[dict]] = {}
    for model in models:
        print(f"\n== {model}: pull (jika belum ada) ==", flush=True)
        if run(["ollama", "pull", model], 3600).returncode != 0:
            print(f"  gagal pull {model}; dilewati")
            continue
        run(["ollama", "run", model, "--keepalive", "30m", "ok"], 600)  # pemanasan, di luar waktu ukur
        results[model] = []
        for task in TASKS:
            print(f"  tugas {task['name']} ...", flush=True)
            row = bench_task(model, task, timeout)
            results[model].append(row)
            print(f"    {'LULUS' if row['ok'] else 'GAGAL'} {row['seconds']}s | {row['note'][:80]}")

    print("\n" + "model".ljust(34) + "lulus  total_detik  rata2_detik/lulus")
    for model, rows in results.items():
        passed = [r for r in rows if r["ok"]]
        total = sum(r["seconds"] for r in rows)
        avg = f"{sum(r['seconds'] for r in passed) / len(passed):.0f}" if passed else "-"
        print(f"{model.ljust(34)}{len(passed)}/{len(rows)}    {total:>10.0f}  {avg:>12}")
    out = ROOT / "logs" / f"benchmark-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDetail: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
