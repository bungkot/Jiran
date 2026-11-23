"""FASE 2 — Sandbox path: semua operasi file wajib berada di dalam workspace."""

from __future__ import annotations

from pathlib import Path

# Folder yang tidak boleh DITULIS oleh agent (dibuat/dikelola tool lain)
DENY_WRITE_PARTS = frozenset({"node_modules", ".git", ".next"})
SAFE_ENV_FILES = frozenset({".env.example", ".env.sample", ".env.template"})
SENSITIVE_NAMES = frozenset(
    {
        ".git",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
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
SENSITIVE_SUFFIXES = frozenset({".jks", ".keystore", ".pem", ".key", ".p12", ".pfx"})


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

    def relative(self, p: Path) -> str:
        try:
            rel = p.resolve().relative_to(self.root)
        except ValueError:
            # Symlink escape: fall back to the link's own name so callers
            # still get a usable string instead of crashing.
            rel = Path(p.name)
        return rel.as_posix() or "."
