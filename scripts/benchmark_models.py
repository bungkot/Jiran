#!/usr/bin/env python3
"""Benchmark model Ollama pada agent ini, dengan penilaian mesin (--verify).

Pemakaian (dari folder proyek, Ollama dan Docker aktif):

    python scripts/benchmark_models.py qwen3.5:4b gemma4:e4b qwen2.5-coder:7b

Setiap model menjalankan 3 tugas kecil lewat `coding-agent goal` (mode lokal).
Hasil dicetak sebagai tabel dan disimpan ke logs/benchmark-<waktu>.json.
Model yang belum ada akan di-pull otomatis. Tidak ada data yang dikirim ke luar.

Exit code 0 hanya bila semua tugas yang dijalankan LULUS dan tidak ada model
yang gagal di-pull. Skrip bisa dipakai sebagai gate regresi di CI.

BENCH_WARMUP_BASE_URL = alamat Ollama yang dipakai untuk pemanasan; arahkan ke
server yang sama dengan OLLAMA_BASE_URL di dalam container agent agar waktu
tugas tidak terpengaruh cold-load model.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess  # noqa: S404
import sys
import time
import urllib.request
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
            "(dataclass Item name,qty,price) dan inventory/store.py "
            "(kelas Store: add, total_value)."
        ),
        "verify": (
            'python -c "from inventory.store import Store; from inventory.models import Item; '
            "s=Store(); s.add(Item('a',2,1.5)); assert s.total_value()==3.0\""
        ),
        "files": ["inventory/store.py", "inventory/models.py"],
    },
]


def run(
    cmd: list[str], timeout: int, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env, check=False
    )


def _ollama_base() -> str:
    return os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")


def _warmup_base() -> str:
    return os.environ.get("BENCH_WARMUP_BASE_URL", _ollama_base()).rstrip("/")


def _post_ollama(path: str, payload: dict, timeout: int, *, base_url: str | None = None) -> dict:
    request = urllib.request.Request(  # noqa: S310 - URL berasal dari env lokal
        (base_url or _ollama_base()) + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read())


def ensure_model(model: str) -> bool:
    """Pastikan model tersedia via HTTP API (Ollama bisa berjalan di container)."""
    try:
        with urllib.request.urlopen(  # noqa: S310 - URL berasal dari env lokal
            _ollama_base() + "/api/tags", timeout=15
        ) as response:
            data = json.loads(response.read())
        names = {
            item.get("name")
            for item in data.get("models", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if model in names or f"{model}:latest" in names:
            return True
    except (OSError, ValueError) as exc:
        print(f"  gagal membaca /api/tags: {exc}")
        return False

    print(f"  menarik {model} lewat API...")
    try:
        reply = _post_ollama("/api/pull", {"name": model, "stream": False}, 3600)
    except (OSError, ValueError) as exc:
        print(f"  gagal pull {model}: {exc}")
        return False
    if isinstance(reply, dict) and reply.get("error"):
        print(f"  gagal pull {model}: {reply['error']}")
        return False
    return True


def warmup(model: str) -> bool:
    """Pemanasan model di luar waktu ukur. Return True bila berhasil."""
    base = _warmup_base()
    try:
        _post_ollama(
            "/api/generate",
            {"model": model, "prompt": "ok", "stream": False, "keep_alive": "30m"},
            600,
            base_url=base,
        )
    except (OSError, ValueError) as exc:
        print(f"  pemanasan dilewati ({base}): {exc}")
        return False
    print(f"  pemanasan selesai di {base}")
    return True


def _agent_container() -> str | None:
    """Cari nama container agent yang sedang berjalan.

    Compose canonical memakai service ``agent``; deployment lama memakai
    ``jiran``. Keduanya didukung, dan ``BENCH_AGENT_CONTAINER`` bisa dipakai
    untuk nama kustom.
    """
    configured = os.environ.get("BENCH_AGENT_CONTAINER", "").strip()
    if configured:
        return configured
    docker = shutil.which("docker")
    if not docker:
        return None
    try:
        listed = run([docker, "ps", "--format", "{{.Names}}"], 15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listed.returncode != 0:
        return None
    names = set(listed.stdout.split())
    for candidate in ("agent", "jiran"):
        if candidate in names:
            return candidate
    return None


def _agent_command(model: str) -> list[str]:
    """Jalankan coding-agent di container bila ada; fallback ke host jika tidak."""
    docker = shutil.which("docker")
    container = _agent_container()
    if docker and container:
        return [
            docker,
            "exec",
            "-e",
            f"OLLAMA_MODEL={model}",
            "-e",
            "OLLAMA_BASE_URL=http://ollama:11434",
            container,
            "coding-agent",
        ]
    return [sys.executable, "-m", "agent.main"]


def bench_task(model: str, task: dict, timeout: int) -> dict:
    project = f"bench-{model.replace(':', '-').replace('/', '-')}-{task['name']}-{int(time.time())}"
    cmd = [
        *_agent_command(model),
        "goal",
        "--json",
        "--project",
        project,
        "--max-iterations",
        "3",
        "--verify",
        task["verify"],
    ]
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
            "task": task["name"],
            "ok": bool(payload.get("achieved")),
            "seconds": round(elapsed, 1),
            "iterations": payload.get("iterations"),
            "tool_calls": payload.get("tool_calls"),
            "note": (payload.get("reason") or payload.get("error") or proc.stderr[-400:]).strip(),
        }
    except subprocess.TimeoutExpired:
        return {
            "task": task["name"],
            "ok": False,
            "seconds": float(timeout),
            "iterations": None,
            "tool_calls": None,
            "note": f"timeout {timeout}s",
        }


def main(models: list[str]) -> int:
    if not models:
        print(__doc__)
        return 1
    timeout = int(os.environ.get("BENCH_TASK_TIMEOUT_S", "1800"))
    results: dict[str, list[dict]] = {}
    for model in models:
        print(f"\n== {model}: pull (jika belum ada) ==", flush=True)
        if not ensure_model(model):
            print(f"  gagal pull {model}; dilewati")
            continue
        warm = warmup(model)  # di luar waktu ukur
        results[model] = []
        for task in TASKS:
            print(f"  tugas {task['name']} ...", flush=True)
            row = bench_task(model, task, timeout)
            row["warm"] = warm
            results[model].append(row)
            print(f"    {'LULUS' if row['ok'] else 'GAGAL'} {row['seconds']}s | {row['note'][:80]}")

    print("\n" + "model".ljust(34) + "lulus/n   total_s   rata2_lulus   stdev   warm")
    for model, rows in results.items():
        passed = [r["seconds"] for r in rows if r["ok"]]
        total = sum(r["seconds"] for r in rows)
        avg = f"{statistics.mean(passed):.0f}" if passed else "-"
        stdev = f"{statistics.pstdev(passed):.0f}" if len(passed) > 1 else "-"
        warm = "ya" if all(r["warm"] for r in rows) else "tidak"
        print(
            f"{model.ljust(34)}{len(passed)}/{len(rows):<7} {total:>8.0f} "
            f"{avg:>12}   {stdev:>5}   {warm}"
        )
    out = ROOT / "logs" / f"benchmark-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDetail: {out}")
    failures = sum(1 for rows in results.values() for row in rows if not row["ok"])
    skipped = len(models) - len(results)
    if failures or skipped:
        print(f"GATE GAGAL: {failures} tugas gagal, {skipped} model dilewati.")
        return 1
    print("GATE LULUS: semua tugas berhasil.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
