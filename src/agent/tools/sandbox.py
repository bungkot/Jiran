"""FASE 2 — Sandbox path: semua operasi file wajib berada di dalam workspace."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

# Folder yang tidak boleh DITULIS oleh agent (dibuat/dikelola tool lain)
DENY_WRITE_PARTS = frozenset({"node_modules", ".git", ".next"})
SAFE_ENV_FILES = frozenset({".env.example", ".env.sample", ".env.template"})
SENSITIVE_NAMES = frozenset(
    {
        ".git",
        ".npmrc",
        ".npm_token",
        ".pypirc",
        ".netrc",
        "credentials",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "authorized_keys",
        "secrets.yaml",
        "secrets.yml",
        "secrets.json",
        "secrets.toml",
    }
)
SENSITIVE_DIRECTORIES = frozenset(
    {
        ".aws",
        ".azure",
        ".docker",
        ".gnupg",
        ".kube",
        ".ssh",
    }
)
SENSITIVE_SUFFIXES = frozenset({".jks", ".keystore", ".pem", ".key", ".p12", ".pfx", ".secret"})


def is_sensitive_relative_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    lowered = [part.lower() for part in parts]
    name = lowered[-1]
    if any(
        part == ".git"
        or part in SENSITIVE_DIRECTORIES
        or part in SENSITIVE_NAMES
        or (part.startswith(".env") and part not in SAFE_ENV_FILES)
        for part in lowered
    ):
        return True
    if name in SAFE_ENV_FILES:
        return False
    if name in SENSITIVE_NAMES or name.startswith(".env"):
        return True
    return any(name.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)


class SandboxError(Exception):
    """Dilempar saat path/perintah melanggar aturan sandbox."""


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str | Path = ".") -> Path:
        """Ubah path (relatif/absolut) jadi path absolut DI DALAM workspace.

        Menangkap: '../', path absolut di luar root, null byte, dan symlink escape
        (karena Path.resolve() mengikuti symlink yang sudah ada).
        """
        raw = str(path)
        if "\x00" in raw:
            raise SandboxError("path mengandung null byte")
        resolved = (self.root / raw).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise SandboxError(f"path di luar workspace ditolak: {raw!r}")
        return resolved

    def resolve_for_write(self, path: str | Path) -> Path:
        resolved = self.resolve(path)
        if resolved == self.root:
            raise SandboxError("tidak bisa menulis ke root workspace")
        rel_parts = resolved.relative_to(self.root).parts
        blocked = DENY_WRITE_PARTS.intersection(rel_parts)
        if blocked:
            raise SandboxError(f"menulis ke {sorted(blocked)} dilarang")
        return resolved

    def write_bytes(self, path: str | Path, data: bytes, *, min_free_disk_mb: int = 0) -> None:
        """Tulis file tanpa memberi ruang untuk symlink swap (TOCTOU).

        `resolve()` hanya memeriksa sekali sebelum file dibuka. Antara pemeriksaan
        dan `write_bytes()`, penyerang (atau agent melalui run_command) bisa
        mengganti target dengan symlink keluar workspace. Di sini file dibuka
        dengan ``O_NOFOLLOW`` lalu inode dari fd dibandingkan dengan ``lstat``
        path: bila berbeda, penulisan dibatalkan.
        """
        target = self.resolve_for_write(path)
        if target.exists() and target.is_dir():
            raise SandboxError(f"target adalah folder: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if min_free_disk_mb > 0:
            try:
                free_mb = shutil_disk_free_mb(target.parent)
            except OSError as exc:
                raise SandboxError(f"gagal memeriksa ruang disk: {exc}") from exc
            if free_mb < min_free_disk_mb:
                raise SandboxError(
                    f"ruang disk tidak cukup ({free_mb:.0f} MB < {min_free_disk_mb} MB)"
                )
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(target, flags, 0o600)
        except OSError as exc:
            raise SandboxError(f"gagal membuka file untuk ditulis: {exc}") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise SandboxError("target bukan file reguler")
            latest = os.lstat(target)
            if stat.S_ISLNK(latest.st_mode) or (
                latest.st_dev,
                latest.st_ino,
            ) != (opened.st_dev, opened.st_ino):
                raise SandboxError("file diganti saat penulisan; ulangi operasi")
            with os.fdopen(fd, "wb") as stream:
                fd = -1  # fdopen mengambil kepemilikan fd
                stream.write(data)
        finally:
            if fd >= 0:
                os.close(fd)

    def relative(self, p: Path) -> str:
        try:
            rel = p.resolve().relative_to(self.root)
        except ValueError:
            # Symlink escape: fall back to the link's own name so callers
            # still get a usable string instead of crashing.
            rel = Path(p.name)
        return rel.as_posix() or "."


def shutil_disk_free_mb(path: Path) -> float:
    """Ruang disk bebas dalam MB untuk path yang diberikan."""
    return shutil.disk_usage(path).free / (1024 * 1024)
