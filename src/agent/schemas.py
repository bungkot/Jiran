"""Skema data (Pydantic) yang dipakai lintas fase."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class CheckStatus(str, Enum):
    OK = "ok"
    WARN = "warn"  # tidak memblokir, tapi perlu perhatian
    FAIL = "fail"  # memblokir agent


class PreflightCheck(BaseModel):
    name: str
    status: CheckStatus
    detail: str = ""
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.status != CheckStatus.FAIL


class PreflightReport(BaseModel):
    checks: list[PreflightCheck] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Gate Fase 0: tidak ada satu pun cek berstatus FAIL."""
        return all(c.status != CheckStatus.FAIL for c in self.checks)

    @property
    def failures(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status == CheckStatus.FAIL]

    @property
    def warnings(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status == CheckStatus.WARN]


class LLMPingResult(BaseModel):
    ok: bool
    latency_s: float = 0.0
    response: str = ""
    error: str = ""


class CommandResult(BaseModel):
    argv: list[str]
    exit_code: int | None = None
    timed_out: bool = False
    truncated: bool = False
    output: str = ""
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def format(self) -> str:
        """Bentuk teks ringkas untuk dikembalikan ke LLM."""
        status = "TIMEOUT" if self.timed_out else f"exit_code={self.exit_code}"
        note = " (output dipotong, hanya bagian akhir)" if self.truncated else ""
        return f"[{status}] {' '.join(self.argv)} ({self.duration_s:.1f}s){note}\n{self.output}"
