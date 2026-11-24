#!/usr/bin/env python3
"""Pangkas artefak log agent yang sudah tua.

Pemakaian:

    python scripts/cleanup_logs.py --days 14
    python scripts/cleanup_logs.py --days 14 --dry-run

File yang dipangkas: ``run-*.jsonl``, ``benchmark-*.json``, dan ``jobs/job-*.json``.
Lewati file yang lebih baru dari batas hari. Tidak menyentuh file lain.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ("logs/run-*.jsonl", "logs/benchmark-*.json", "logs/jobs/job-*.json")


def collect(logs_dir: Path, days: int) -> list[Path]:
    cutoff = time.time() - days * 24 * 60 * 60
    found: list[Path] = []
    for pattern in PATTERNS:
        for path in logs_dir.parent.glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    found.append(path)
            except OSError:
                continue
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="usia maksimum log (hari)")
    parser.add_argument("--dry-run", action="store_true", help="tampilkan saja, jangan hapus")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days harus >= 1")

    targets = collect(ROOT / "logs", args.days)
    for path in targets:
        print(("akan dihapus" if args.dry_run else "menghapus") + f": {path}")
        if not args.dry_run:
            try:
                path.unlink()
            except OSError as exc:
                print(f"  gagal: {exc}")
    print(f"{len(targets)} file {'akan dihapus' if args.dry_run else 'dihapus'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
