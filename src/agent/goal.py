"""Goal loop: buat proyek baru dan ulangi worker agent sampai tujuan tercapai.

Lapisan ini berada di atas ``run_agent``. Worker agent mengerjakan satu putaran
tool-calling; goal loop memeriksa hasilnya dengan kriteria penerimaan berupa
perintah verifikasi dan file wajib, lalu meminta penilaian model apakah tujuan
benar-benar tercapai. Selama belum tercapai, loop memberi umpan balik ke putaran
berikutnya sampai tujuan tercapai, mandek, atau batas putaran habis.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from ollama import ResponseError

from .agent import (
    AgentError,
    AgentResult,
    Verification,
    _content_to_text,
    _decode_json_payloads,
    _messages_for_log,
    _RunLog,
    run_agent,
)
from .config import Settings
from .llm import get_llm
from .schemas import CommandResult
from .tools import Sandbox, SandboxError
from .tools.container_runner import run_command_in_container
from .tools.sandbox import is_sensitive_relative_path

VerifyFn = Callable[..., CommandResult]

_IGNORED_TREE_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        "target",
    }
)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

_JUDGE_PROMPT = """You evaluate whether a coding goal has actually been achieved.

Decide based on evidence, not on promises. The goal is achieved only when the
agent's summary, the project files, AND every machine check support it. If a
required file is missing or a verification command failed, answer complete=false.
Reply with ONLY one JSON object, no prose:
{"complete": true, "reason": "one short sentence in the user's language"}"""


class GoalError(RuntimeError):
    """Goal loop tidak dapat dijalankan dengan aman."""


@dataclass(frozen=True)
class GoalAcceptance:
    """Kriteria penerimaan yang diukur mesin, di luar penilaian model."""

    verification_commands: tuple[str, ...] = ()
    required_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class AcceptanceCheck:
    complete: bool
    reason: str
    verification_ok: bool
    required_files_ok: bool
    judge_ok: bool
    verifications: tuple[Verification, ...] = ()
    missing_files: tuple[str, ...] = ()

    @property
    def machine_ok(self) -> bool:
        return self.verification_ok and self.required_files_ok


@dataclass(frozen=True)
class GoalIteration:
    index: int
    result: AgentResult
    acceptance: AcceptanceCheck


@dataclass(frozen=True)
class GoalResult:
    goal: str
    project: str
    session_id: str
    iterations: tuple[GoalIteration, ...]
    achieved: bool
    reason: str

    @property
    def files_written(self) -> int:
        return sum(item.result.files_written for item in self.iterations)

    @property
    def steps(self) -> int:
        return sum(item.result.steps for item in self.iterations)

    @property
    def tool_calls(self) -> int:
        return sum(item.result.tool_calls for item in self.iterations)

    @property
    def response(self) -> str:
        return self.iterations[-1].result.response if self.iterations else ""

    @property
    def verifications(self) -> tuple[Verification, ...]:
        return self.iterations[-1].acceptance.verifications if self.iterations else ()


def project_slug(text: str, max_len: int = 40) -> str:
    """Buat nama folder aman dari teks tujuan."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_len].strip("-")
    return slug or "project"


def resolve_project_dir(settings: Settings, goal: str, project_name: str | None = None) -> Path:
    """Buat folder proyek baru di workspace, atau pakai nama yang diberikan.

    Tanpa ``project_name`` nama diambil dari tujuan dan diberi akhiran agar tidak
    menimpa proyek yang sudah ada. Dengan ``project_name`` folder dibuat bila
    belum ada, atau dipakai kembali bila sudah ada.
    """
    workspace = settings.workspace_dir.resolve()
    sandbox = Sandbox(workspace)
    if project_name is not None:
        name = project_name.strip()
        if not name:
            raise GoalError("Nama proyek tidak boleh kosong.")
        try:
            target = sandbox.resolve_for_write(name)
        except SandboxError as exc:
            raise GoalError(f"Nama proyek tidak valid: {exc}") from exc
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GoalError(f"Gagal membuat folder proyek {target}: {exc}") from exc
        return target

    base = project_slug(goal)
    candidate = workspace / base
    suffix = 2
    while candidate.exists():
        candidate = workspace / f"{base}-{suffix}"
        suffix += 1
    try:
        candidate.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise GoalError(f"Gagal membuat folder proyek {candidate}: {exc}") from exc
    return candidate


def _visible_files(project: Path) -> list[str]:
    files: list[str] = []
    for path in project.rglob("*"):
        rel = path.relative_to(project).as_posix()
        parts = rel.split("/")
        if any(part in _IGNORED_TREE_DIRS for part in parts) or is_sensitive_relative_path(rel):
            continue
        if path.is_file():
            files.append(rel)
    return sorted(files)


def _project_tree(project: Path, limit: int = 80) -> str:
    files = _visible_files(project)
    if not files:
        return "(folder proyek kosong)"
    if len(files) > limit:
        return "\n".join(files[:limit]) + f"\n...[+{len(files) - limit} file lain]"
    return "\n".join(files)


def _project_fingerprint(project: Path) -> str:
    digest = hashlib.sha256()
    for path in project.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(project).as_posix()
        parts = rel.split("/")
        if any(part in _IGNORED_TREE_DIRS for part in parts) or is_sensitive_relative_path(rel):
            continue
        digest.update(f"{rel}\n".encode())
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            digest.update(f"[unreadable:{type(exc).__name__}]\n".encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _judge(
    goal: str,
    project: Path,
    result: AgentResult,
    check_parts: tuple[tuple[Verification, ...], tuple[str, ...]],
    judge_model: Any,
    settings: Settings,
    log: _RunLog,
    session_id: str,
) -> tuple[bool, str]:
    verifications, missing = check_parts
    verification_text = "\n".join(
        f"{item.command}: {'LULUS' if item.result.ok else 'GAGAL'}\n{item.result.format()}"
        for item in verifications
    ) or "(tidak ada perintah verifikasi)"
    payload = (
        f"TUJUAN:\n{goal}\n\n"
        f"RINGKASAN AGENT PUTARAN INI:\n{result.response or '(tidak ada)'}\n\n"
        f"FILE PROYEK:\n{_project_tree(project)}\n\n"
        f"HASIL VERIFIKASI:\n{verification_text}\n\n"
        f"FILE WAJIB YANG HILANG:\n{', '.join(missing) or '(tidak ada)'}"
    )
    messages = [
        SystemMessage(content=_JUDGE_PROMPT),
        HumanMessage(content=payload),
    ]
    last_error = "format JSON tidak ditemukan"
    for attempt in range(settings.llm_max_retries + 1):
        try:
            log.event(
                "judge.request",
                session_id=session_id,
                attempt=attempt + 1,
                messages=_messages_for_log(messages),
            )
            response = judge_model.invoke(messages)
            raw_content = getattr(response, "content", response)
            text = _content_to_text(raw_content).strip()
            log.event(
                "judge.response",
                session_id=session_id,
                attempt=attempt + 1,
                content=raw_content,
                usage_metadata=getattr(response, "usage_metadata", None),
                response_metadata=getattr(response, "response_metadata", None),
            )
            text = _THINK_RE.sub("", text).strip()
            for value in _decode_json_payloads(text):
                if not isinstance(value, dict) or "complete" not in value:
                    continue
                raw = value["complete"]
                if isinstance(raw, bool):
                    complete = raw
                else:
                    complete = str(raw).strip().lower() in {"true", "yes", "1"}
                return complete, str(value.get("reason", "")).strip()
            last_error = "model tidak mengembalikan objek JSON dengan field 'complete'"
        except (httpx.HTTPError, ResponseError, TimeoutError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.event(
                "judge.error",
                session_id=session_id,
                attempt=attempt + 1,
                error_type=type(exc).__name__,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - judge gagal harus jadi penilaian, bukan crash
            last_error = f"{type(exc).__name__}: {exc}"
            log.event(
                "judge.error",
                session_id=session_id,
                attempt=attempt + 1,
                error_type=type(exc).__name__,
                error=str(exc),
            )
        if attempt < settings.llm_max_retries:
            time.sleep(min(2**attempt, 5))
    return False, f"Penilaian model gagal: {last_error}"


def _evaluate(
    goal: str,
    project: Path,
    result: AgentResult,
    acceptance: GoalAcceptance,
    settings: Settings,
    judge_model: Any,
    run_verify: VerifyFn,
    log: _RunLog,
    session_id: str,
    on_output: Callable[[str], None] | None = None,
    use_default_verify: bool = False,
) -> AcceptanceCheck:
    sandbox = Sandbox(project)
    verifications: list[Verification] = []
    verification_ok = True
    for command in acceptance.verification_commands:
        log.event(
            "goal.verification.started",
            session_id=session_id,
            command=command,
        )
        try:
            if use_default_verify:
                command_result = run_command_in_container(
                    command, sandbox, settings, on_output=on_output
                )
            else:
                command_result = run_verify(command, sandbox, settings)
        except SandboxError as exc:
            command_result = CommandResult(argv=[command], output=f"ERROR: {exc}")
        verification = Verification(command, command_result)
        verifications.append(verification)
        verification_ok = verification_ok and command_result.ok
        log.event(
            "goal.verification.completed",
            session_id=session_id,
            command=command,
            ok=command_result.ok,
            exit_code=command_result.exit_code,
            result=command_result.format(),
        )

    missing: list[str] = []
    for relative in acceptance.required_files:
        try:
            target = sandbox.resolve(relative)
        except SandboxError:
            missing.append(relative)
            continue
        if not target.is_file():
            missing.append(relative)

    judge_ok, reason = _judge(
        goal,
        project,
        result,
        (tuple(verifications), tuple(missing)),
        judge_model,
        settings,
        log,
        session_id,
    )
    log.event(
        "goal.acceptance.checked",
        session_id=session_id,
        verification_ok=verification_ok,
        missing_files=missing,
        judge_ok=judge_ok,
        reason=reason,
    )
    if not result.finished:
        judge_ok = False
        reason = f"{reason} (putaran agent belum selesai)".strip()
    return AcceptanceCheck(
        complete=verification_ok and not missing and judge_ok,
        reason=reason
        or ("Tujuan tercapai." if judge_ok else "Model menilai tujuan belum tercapai."),
        verification_ok=verification_ok,
        required_files_ok=not missing,
        judge_ok=judge_ok,
        verifications=tuple(verifications),
        missing_files=tuple(missing),
    )


def _worker_prompt(
    goal: str,
    index: int,
    history: tuple[GoalIteration, ...],
    feedback: str,
) -> str:
    if index == 1:
        return (
            f"TUJUAN PROYEK: {goal}\n\n"
            "Ini adalah putaran pertama. Folder proyek masih kosong: buat proyek ini DARI AWAL. "
            "Buat struktur dan file minimal yang lengkap dan saling konsisten. Sertakan manifest "
            "yang sesuai dengan stack (pyproject.toml/requirements.txt untuk Python, "
            "package.json untuk Node, go.mod untuk Go, dan sebagainya) agar image toolchain "
            "dapat terdeteksi. Lalu jalankan pemeriksaan relevan dengan run_command. Jangan "
            "berhenti hanya karena toolchain belum terdeteksi: buat manifest yang sesuai. "
            "Jangan hanya merencanakan; terapkan file dengan write_file."
        )
    recent = history[-3:]
    summary = "\n".join(
        f"- Putaran {item.index}: {item.result.response[:250] or '(tanpa ringkasan)'} "
        f"({item.result.files_written} file ditulis)"
        for item in recent
    )
    return (
        f"TUJUAN PROYEK: {goal}\n\n"
        f"Putaran ke-{index}. Proyek sudah berisi file dari putaran sebelumnya; lanjutkan "
        "dari kondisi tersebut dan lakukan perubahan minimum.\n\n"
        f"RIWAYAT SINGKAT:\n{summary}\n\n"
        f"YANG MASIH PERLU DIPERBAIKI:\n{feedback or 'Tinjau kembali sisa pekerjaan.'}\n\n"
        "Perbaiki masalah di atas dengan write_file, jalankan kembali pemeriksaan relevan "
        "dengan run_command, lalu beri ringkasan singkat."
    )


def _feedback(check: AcceptanceCheck, result: AgentResult) -> str:
    parts: list[str] = []
    if not check.verification_ok:
        failed = [item for item in check.verifications if not item.result.ok]
        parts.append(
            "Perintah verifikasi gagal:\n"
            + "\n".join(f"{item.command}\n{item.result.format()}" for item in failed)
        )
    if check.missing_files:
        parts.append("File wajib belum ada: " + ", ".join(check.missing_files))
    if not check.judge_ok:
        parts.append(f"Penilaian tujuan: {check.reason}")
    if not result.finished:
        parts.append("Putaran agent sebelumnya berhenti sebelum selesai.")
    return "\n\n".join(parts) or "Belum ada kegagalan spesifik; tinjau kembali tujuan."


def run_goal_loop(
    goal: str,
    settings: Settings,
    *,
    project_name: str | None = None,
    acceptance: GoalAcceptance | None = None,
    max_iterations: int | None = None,
    llm: Any | None = None,
    judge: Any | None = None,
    run_verify: VerifyFn | None = None,
    allow_no_changes: bool = False,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    on_output: Callable[[str], None] | None = None,
) -> GoalResult:
    """Buat proyek dan jalankan worker agent berulang sampai tujuan tercapai.

    ``llm`` dipakai worker agent, ``judge`` menilai pencapaian tujuan (defaultnya
    memakai ``llm`` atau model dari pengaturan), dan ``run_verify`` dapat
    diganti untuk keperluan pengujian.
    """
    target_goal = goal.strip()
    if not target_goal:
        raise GoalError("Tujuan tidak boleh kosong.")
    limit = settings.max_goal_iterations if max_iterations is None else max_iterations
    if limit < 1:
        raise GoalError("Batas putaran goal harus lebih besar dari 0.")

    project = resolve_project_dir(settings, target_goal, project_name)
    workspace = settings.workspace_dir.resolve()
    relative = project.relative_to(workspace).as_posix()
    criteria = acceptance or GoalAcceptance()
    verify_fn = run_verify or run_command_in_container
    judge_model = judge or llm or get_llm(settings)
    session_id = uuid.uuid4().hex
    log = _RunLog(settings.logs_dir, session_id, on_event)
    log.event(
        "goal.started",
        session_id=session_id,
        goal=target_goal,
        project=relative,
        max_iterations=limit,
        verification_count=len(criteria.verification_commands),
        verification_commands=criteria.verification_commands,
        required_file_count=len(criteria.required_files),
        required_files=criteria.required_files,
    )

    history: tuple[GoalIteration, ...] = ()
    feedback = ""
    signature: tuple[str, ...] | None = None
    stuck = 0

    for index in range(1, limit + 1):
        prompt = _worker_prompt(target_goal, index, history, feedback)
        log.event(
            "goal.iteration.started",
            session_id=session_id,
            iteration=index,
            prompt=prompt,
            feedback=feedback,
        )
        try:
            result = run_agent(
                prompt,
                project,
                settings,
                llm=llm,
                allow_no_changes=allow_no_changes,
                on_event=on_event,
                on_output=on_output,
            )
        except AgentError as exc:
            log.event(
                "goal.iteration.failed",
                session_id=session_id,
                iteration=index,
                error_type=type(exc).__name__,
            )
            return GoalResult(
                target_goal,
                relative,
                session_id,
                history,
                False,
                f"Putaran {index} gagal: {exc}",
            )

        check = _evaluate(
            target_goal,
            project,
            result,
            criteria,
            settings,
            judge_model,
            verify_fn,
            log,
            session_id,
            on_output,
            run_verify is None,
        )
        history += (GoalIteration(index=index, result=result, acceptance=check),)
        log.event(
            "goal.iteration.completed",
            session_id=session_id,
            iteration=index,
            complete=check.complete,
            reason=check.reason,
            worker_response=result.response,
            worker_finished=result.finished,
            files_written=result.files_written,
            steps=result.steps,
        )

        if check.complete:
            log.event("goal.completed", session_id=session_id, iterations=index)
            return GoalResult(target_goal, relative, session_id, history, True, check.reason)

        current = (
            _project_fingerprint(project),
            *(str(item.result.ok) for item in check.verifications),
            *check.missing_files,
        )
        if signature is not None and current == signature:
            stuck += 1
        else:
            stuck = 0
        signature = current
        if stuck >= settings.goal_stuck_limit:
            reason = "Tidak ada kemajuan pada proyek setelah beberapa putaran."
            log.event("goal.failed", session_id=session_id, reason="stuck")
            return GoalResult(target_goal, relative, session_id, history, False, reason)

        feedback = _feedback(check, result)

    reason = f"Batas {limit} putaran tercapai sebelum tujuan terpenuhi."
    log.event("goal.failed", session_id=session_id, reason="iteration_limit")
    return GoalResult(target_goal, relative, session_id, history, False, reason)
