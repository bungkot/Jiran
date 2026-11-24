"""Tool edit presisi dan pencarian kode.

``write_file`` menimpa seluruh file, sehingga perubahan kecil memaksa model menulis
ulang semuanya (boros token dan rawan merusak kode yang sudah benar). ``edit_file``
mengganti satu potongan teks yang unik, ``search_files`` mencari teks/regex di
seluruh proyek. Kegagalan dikembalikan sebagai teks ``ERROR: ...`` agar model bisa
memperbaiki langkahnya.
"""

from __future__ import annotations

import fnmatch
import os
import re

from langchain_core.tools import BaseTool, tool

from .sandbox import Sandbox, SandboxError, is_sensitive_relative_path

SEARCH_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        "build",
        "dist",
        "target",
        "vendor",
    }
)
MAX_SEARCH_RESULTS = 100
MAX_LINE_CHARS = 300


def build_edit_tools(
    sandbox: Sandbox, max_file_bytes: int = 200_000, min_free_disk_mb: int = 0
) -> list[BaseTool]:
    @tool
    def edit_file(path: str, old_str: str, new_str: str, replace_all: bool = False) -> str:
        """Ganti teks di file yang SUDAH ada. `old_str` harus persis sama dengan isi file
        (termasuk spasi/indentasi) dan unik, kecuali `replace_all` true. Lebih disukai
        daripada write_file untuk perubahan kecil. Baca file dulu agar `old_str` akurat."""
        try:
            target = sandbox.resolve_for_write(path)
            if is_sensitive_relative_path(sandbox.relative(target)):
                return "ERROR: mengedit file sensitif dilarang"
            if not target.is_file():
                return f"ERROR: file tidak ditemukan: {path!r} (gunakan write_file untuk file baru)"
            if not old_str:
                return "ERROR: old_str tidak boleh kosong"
            if old_str == new_str:
                return "ERROR: old_str dan new_str identik; tidak ada perubahan"
            size = target.stat().st_size
            if size > max_file_bytes:
                return f"ERROR: file terlalu besar untuk diedit ({size} > {max_file_bytes} byte)"
            text = target.read_text(encoding="utf-8")
        except SandboxError as exc:
            return f"ERROR: {exc}"
        except UnicodeDecodeError:
            return "ERROR: file bukan teks UTF-8 (kemungkinan biner)"
        except OSError as exc:
            return f"ERROR: gagal membaca file: {exc}"

        count = text.count(old_str)
        if count == 0:
            return (
                "ERROR: old_str tidak ditemukan. Baca ulang file dengan read_file dan salin "
                "teks persis, termasuk indentasi."
            )
        if count > 1 and not replace_all:
            return (
                f"ERROR: old_str muncul {count} kali. Tambahkan baris di sekitarnya agar unik, "
                "atau set replace_all=true."
            )
        updated = text.replace(old_str, new_str, -1 if replace_all else 1)
        data = updated.encode("utf-8")
        if len(data) > max_file_bytes:
            return f"ERROR: hasil edit terlalu besar ({len(data)} > {max_file_bytes} byte)"
        try:
            sandbox.write_bytes(target, data, min_free_disk_mb=min_free_disk_mb)
        except SandboxError as exc:
            return f"ERROR: {exc}"
        except OSError as exc:
            return f"ERROR: gagal menulis file: {exc}"
        replaced = count if replace_all else 1
        return f"OK: {replaced} penggantian di {sandbox.relative(target)}"

    @tool
    def search_files(
        pattern: str,
        path: str = ".",
        glob: str = "",
        regex: bool = False,
        ignore_case: bool = True,
    ) -> str:
        """Cari teks di file proyek. Hasil berformat `path:baris: isi`. Default pencarian
        substring; set `regex` true untuk regular expression. `glob` membatasi nama file
        (mis. '*.py'). Folder dependency/build dilewati."""
        if not pattern:
            return "ERROR: pattern tidak boleh kosong"
        flags = re.IGNORECASE if ignore_case else 0
        try:
            matcher = re.compile(pattern if regex else re.escape(pattern), flags)
            base = sandbox.resolve(path)
        except re.error as exc:
            return f"ERROR: regex tidak valid: {exc}"
        except SandboxError as exc:
            return f"ERROR: {exc}"
        if not base.exists():
            return f"ERROR: path tidak ditemukan: {path!r}"

        results: list[str] = []
        truncated = False
        walk_roots: list[tuple[str, list[str], list[str]]] | None = (
            [(str(base.parent), [], [base.name])] if base.is_file() else None
        )
        iterator = walk_roots or os.walk(base, followlinks=False)
        for current, dirs, files in iterator:
            dirs[:] = sorted(d for d in dirs if d not in SEARCH_SKIP_DIRS)
            for name in sorted(files):
                file_path = sandbox.root.joinpath(current, name)
                try:
                    relative = file_path.resolve().relative_to(sandbox.root).as_posix()
                except (ValueError, OSError):
                    continue
                if is_sensitive_relative_path(relative) or file_path.is_symlink():
                    continue
                if glob and not (fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(relative, glob)):
                    continue
                try:
                    if file_path.stat().st_size > max_file_bytes:
                        continue
                    lines = file_path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError):
                    continue
                for number, line in enumerate(lines, start=1):
                    if matcher.search(line[:MAX_LINE_CHARS]):
                        results.append(f"{relative}:{number}: {line[:MAX_LINE_CHARS].strip()}")
                        if len(results) >= MAX_SEARCH_RESULTS:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break
        if not results:
            return "(tidak ada hasil)"
        note = f"\n...[dibatasi {MAX_SEARCH_RESULTS} hasil; persempit pencarian]"
        suffix = note if truncated else ""
        return "\n".join(results) + suffix

    return [edit_file, search_files]
