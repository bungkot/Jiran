"""FASE 2 — Tools file (LangChain @tool). Kegagalan dikembalikan sebagai teks 'ERROR: ...'
agar LLM bisa membaca & memperbaiki langkahnya, bukan membuat agent crash."""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from .sandbox import Sandbox, SandboxError, is_sensitive_relative_path

MAX_LIST_ENTRIES = 200


def build_fs_tools(
    sandbox: Sandbox,
    max_file_bytes: int = 200_000,
    max_read_chars: int = 20_000,
    min_free_disk_mb: int = 0,
) -> list[BaseTool]:
    @tool
    def write_file(path: str, content: str) -> str:
        """Tulis (atau timpa) file teks. `path` relatif terhadap workspace,
        mis. 'my-app/src/app/page.tsx'.
        Folder induk dibuat otomatis. Tidak boleh menulis ke node_modules, .git, atau .next."""
        try:
            target = sandbox.resolve_for_write(path)
            if is_sensitive_relative_path(sandbox.relative(target)):
                return "ERROR: menulis ke file sensitif dilarang"
            data = content.encode("utf-8")
            if len(data) > max_file_bytes:
                return f"ERROR: file terlalu besar ({len(data)} > {max_file_bytes} byte)"
            if target.exists() and target.is_dir():
                return f"ERROR: {path!r} adalah folder"
            sandbox.write_bytes(target, data, min_free_disk_mb=min_free_disk_mb)
            return f"OK: menulis {len(data)} byte ke {sandbox.relative(target)}"
        except SandboxError as e:
            return f"ERROR: {e}"
        except OSError as e:
            return f"ERROR: gagal menulis file: {e}"

    @tool
    def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
        """Baca isi file teks di workspace (path relatif). Isi yang terlalu panjang dipotong.
        Untuk file besar, isi `start_line` dan `end_line` (mulai dari 1) agar hanya bagian
        itu yang dibaca, ditampilkan dengan nomor baris."""
        try:
            target = sandbox.resolve(path)
            if is_sensitive_relative_path(sandbox.relative(target)):
                return "ERROR: membaca file sensitif dilarang"
            if not target.exists():
                return f"ERROR: file tidak ditemukan: {path!r}"
            if target.is_dir():
                return f"ERROR: {path!r} adalah folder, gunakan list_dir"
            if start_line or end_line:
                # Baca bertahap: jangan memuat seluruh file hanya untuk satu rentang.
                first = max(start_line, 1)
                last = end_line if end_line > 0 else 0
                if last and last < first:
                    return "ERROR: end_line harus >= start_line"
                collected: list[str] = []
                with target.open("r", encoding="utf-8") as stream:
                    for number, line in enumerate(stream, start=1):
                        if last and number > last:
                            break
                        if number >= first:
                            collected.append(f"{number}: {line.rstrip()}")
                if not collected:
                    return f"ERROR: start_line {first} melebihi jumlah baris file"
                numbered = "\n".join(collected)
                if len(numbered) > max_read_chars:
                    return numbered[:max_read_chars] + "\n...[dipotong, persempit rentang baris]"
                return numbered
            size = target.stat().st_size
            if size > max_file_bytes:
                return (
                    f"ERROR: file terlalu besar untuk dibaca penuh ({size} > "
                    f"{max_file_bytes} byte); gunakan start_line/end_line"
                )
            text = target.read_text(encoding="utf-8")
        except SandboxError as e:
            return f"ERROR: {e}"
        except UnicodeDecodeError:
            return "ERROR: file bukan teks UTF-8 (kemungkinan biner)"
        except OSError as e:
            return f"ERROR: gagal membaca file: {e}"
        if len(text) > max_read_chars:
            return text[:max_read_chars] + f"\n...[dipotong, total {len(text)} karakter]"
        return text

    @tool
    def list_dir(path: str = ".") -> str:
        """Daftar isi satu folder di workspace (tidak rekursif). Folder diberi akhiran '/'."""
        try:
            target = sandbox.resolve(path)
            if is_sensitive_relative_path(sandbox.relative(target)):
                return "ERROR: membaca folder sensitif dilarang"
            if not target.exists():
                return f"ERROR: folder tidak ditemukan: {path!r}"
            if not target.is_dir():
                return f"ERROR: {path!r} bukan folder"
            entries = sorted(
                (
                    entry
                    for entry in target.iterdir()
                    if not is_sensitive_relative_path(sandbox.relative(entry))
                ),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except SandboxError as e:
            return f"ERROR: {e}"
        except OSError as e:
            return f"ERROR: gagal membaca folder: {e}"
        names = [p.name + ("/" if p.is_dir() else "") for p in entries]
        if not names:
            return "(folder kosong)"
        extra = len(names) - MAX_LIST_ENTRIES
        shown = names[:MAX_LIST_ENTRIES]
        return "\n".join(shown) + (f"\n...[+{extra} entri lain]" if extra > 0 else "")

    return [write_file, read_file, list_dir]
