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
    AgentCancelled,
    AgentError,
    AgentResult,
    Verification,
    _compact_tool_result,
    _content_to_text,
    _decode_json_payloads,
    _messages_for_log,
    _response_language,
    _RunLog,
    run_agent,
)
from .config import Settings
from .llm import get_llm
from .schemas import CommandResult, GoalPlan, TodoItem
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

_PLAN_PROMPT = """You split one coding goal into a short sequential todo list.

Reply with ONLY one JSON object, no prose:
{"todos":[{"task":"short concrete instruction","files":["path/to/file"]}, ...]}

Rules:
- Produce between 1 and the stated maximum number of todos.
- Each todo must be a small, independently executable change, ordered by dependency.
- Do NOT include todos that only run tests/verification; acceptance checks run separately.
- Do not invent features beyond the user's goal. Keep the goal's exact requirements.
- Use concrete file paths when the goal implies them; otherwise omit 'files'.
- Keep each 'task' under 300 characters."""


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
    judge_skipped: bool = False

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


def visible_project_files(project: Path) -> list[str]:
    files: list[str] = []
    for path in project.rglob("*"):
        rel = path.relative_to(project).as_posix()
        parts = rel.split("/")
        if any(part in _IGNORED_TREE_DIRS for part in parts) or is_sensitive_relative_path(rel):
            continue
        if path.is_file() and not path.is_symlink():
            files.append(rel)
    return sorted(files)


def _project_tree(project: Path, limit: int = 80) -> str:
    files = visible_project_files(project)
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


def _heuristic_plan(goal: str, criteria: GoalAcceptance, limit: int) -> GoalPlan | None:
    """Buat todo tanpa LLM bila goal/required_files sudah menyebut file jelas."""
    raw_files = [item.strip() for item in criteria.required_files if item.strip()]
    if not raw_files:
        seen: set[str] = set()
        for candidate in re.findall(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9]+", goal):
            if candidate.startswith(("/", ".")) or ".." in candidate:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            raw_files.append(candidate)
            if len(raw_files) >= 5:
                break
    files = raw_files[:limit]
    if len(files) < 2:
        return None

    todos: list[TodoItem] = []
    short_goal = goal if len(goal) <= 180 else goal[:177] + "..."
    for relative in files:
        name = Path(relative).name.lower()
        if name.startswith("test") or "_test" in name:
            task = (
                f"Buat {relative} berisi tes unittest untuk memverifikasi "
                "implementasi yang diminta."
            )
        elif name.startswith("readme"):
            task = f"Buat {relative} berisi deskripsi singkat proyek dan cara menjalankannya."
        else:
            task = f"Buat {relative} sesuai tujuan berikut: {short_goal}"
        todos.append(TodoItem(task=task, files=(relative,)))
    return GoalPlan(todos=todos)


def plan_goal(
    goal: str,
    project: Path,
    acceptance: GoalAcceptance,
    settings: Settings,
    planner_model: Any,
    log: _RunLog,
    session_id: str,
    limit: int,
) -> GoalPlan:
    """Decompose a goal into small todos. Falls back to one full-goal todo."""
    log.event("goal.plan.started", session_id=session_id, max_todos=limit)
    payload = (
        f"TUJUAN:\n{goal}\n\n"
        f"FILE PROYEK:\n{_project_tree(project)}\n\n"
        f"VERIFIKASI WAJIB:\n"
        f"{chr(10).join(acceptance.verification_commands) or '(tidak ada)'}\n\n"
        f"FILE WAJIB:\n"
        f"{chr(10).join(acceptance.required_files) or '(tidak ada)'}\n\n"
        f"MAKSIMAL TODO: {limit}"
    )
    messages = [
        SystemMessage(content=_PLAN_PROMPT),
        HumanMessage(content=payload),
    ]
    last_error = "rencana tidak ditemukan"
    for attempt in range(settings.llm_max_retries + 1):
        try:
            log.event(
                "plan.request",
                session_id=session_id,
                attempt=attempt + 1,
                messages=_messages_for_log(messages),
            )
            response = planner_model.invoke(messages)
            raw_content = getattr(response, "content", response)
            text = _THINK_RE.sub("", _content_to_text(raw_content)).strip()
            log.event(
                "plan.response",
                session_id=session_id,
                attempt=attempt + 1,
                content=raw_content,
            )
            for value in _decode_json_payloads(text):
                if not isinstance(value, dict):
                    continue
                raw_todos = value.get("todos")
                if not isinstance(raw_todos, list):
                    continue
                todos: list[TodoItem] = []
                for raw in raw_todos:
                    if not isinstance(raw, dict):
                        continue
                    task = raw.get("task")
                    if not isinstance(task, str) or not task.strip() or len(task) > 300:
                        continue
                    raw_files = raw.get("files", [])
                    files: tuple[str, ...] = ()
                    if isinstance(raw_files, list) and len(raw_files) <= 20:
                        files = tuple(f for f in raw_files if isinstance(f, str) and f.strip())
                    todos.append(TodoItem(task=task.strip(), files=files))
                    if len(todos) >= limit:
                        break
                if todos:
                    plan = GoalPlan(todos=todos)
                    log.event(
                        "goal.plan.created",
                        session_id=session_id,
                        source="llm",
                        todos=[item.task for item in plan.todos],
                    )
                    return plan
                last_error = "field 'todos' kosong atau tidak valid"
        except (httpx.HTTPError, ResponseError, TimeoutError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.event(
                "plan.error",
                session_id=session_id,
                attempt=attempt + 1,
                error_type=type(exc).__name__,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - planner gagal harus jadi fallback
            last_error = f"{type(exc).__name__}: {exc}"
            log.event(
                "plan.error",
                session_id=session_id,
                attempt=attempt + 1,
                error_type=type(exc).__name__,
                error=str(exc),
            )
        if attempt < settings.llm_max_retries:
            time.sleep(min(2**attempt, 5))
    log.event("goal.plan.fallback", session_id=session_id, reason=last_error)
    return GoalPlan(todos=[TodoItem(task=goal)])


def _todo_prompt(
    goal: str,
    todo: TodoItem,
    index: int,
    total: int,
    completed: tuple[str, ...],
    feedback: str,
) -> str:
    response_language = _response_language(goal)
    language_instruction = (
        f"FINAL RESPONSE LANGUAGE: {response_language}. "
        "Write the final user-facing summary in this language, regardless of the "
        "language used by these internal instructions.\n"
    )
    files = f"\nFILE YANG DISARANKAN: {', '.join(todo.files)}\n" if todo.files else "\n"
    head = (
        f"{language_instruction}TUJUAN PROYEK: {goal}\n\n"
        f"TUGAS {index}/{total}: {todo.task}\n{files}"
    )
    if not completed:
        return (
            head + "Kerjakan HANYA tugas ini. Gunakan tool untuk memeriksa proyek, terapkan "
            "perubahan minimum, lalu ringkas dalam satu atau dua kalimat. Jangan buat file "
            "atau perubahan di luar tugas ini."
        )
    recent = "\n".join(f"- {item}" for item in completed[-3:])
    return (
        head
        + f"TUGAS SEBELUMNYA (ringkas):\n{recent}\n\n"
        + f"YANG MASIH PERLU DIPERBAIKI:\n{feedback or 'Tinjau tugas ini.'}\n\n"
        + "Lanjutkan dari kondisi proyek saat ini. Kerjakan hanya tugas ini, gunakan "
        "write_file/edit_file, lalu beri ringkasan sangat singkat."
    )


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
    verification_text = (
        "\n".join(
            f"{item.command}: {'LULUS' if item.result.ok else 'GAGAL'}\n{item.result.format()}"
            for item in verifications
        )
        or "(tidak ada perintah verifikasi)"
    )
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

    machine_ok = verification_ok and not missing
    has_machine_checks = bool(acceptance.verification_commands or acceptance.required_files)
    judge_skipped = False

    if not result.finished:
        judge_ok = False
        judge_skipped = True
        reason = "Putaran agent belum selesai; judge LLM dilewati."
    elif not machine_ok:
        judge_ok = False
        if not verification_ok:
            reason = "Verifikasi masih gagal."
        else:
            reason = "File wajib belum ada: " + ", ".join(missing)
    elif has_machine_checks:
        # Fast path menghemat latensi judge, tetapi tidak bisa menilai apakah isi
        # file benar-benar memenuhi tujuan. Mode strict (REQUIRE_JUDGE_ON_FAST_PATH)
        # mengharuskan judge tetap dipanggil; statusnya selalu diekspos.
        if settings.require_judge_on_fast_path:
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
        else:
            judge_ok = True
            judge_skipped = True
            reason = "Kriteria mesin terpenuhi; tujuan diterima tanpa judge LLM."
        log.event(
            "goal.acceptance.fast_path",
            session_id=session_id,
            verification_ok=verification_ok,
            missing_files=missing,
            judge_skipped=judge_skipped,
        )
    else:
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
        fast_path=machine_ok and has_machine_checks,
        judge_skipped=judge_skipped,
    )
    return AcceptanceCheck(
        complete=verification_ok and not missing and judge_ok,
        reason=reason
        or ("Tujuan tercapai." if judge_ok else "Model menilai tujuan belum tercapai."),
        verification_ok=verification_ok,
        required_files_ok=not missing,
        judge_ok=judge_ok,
        verifications=tuple(verifications),
        missing_files=tuple(missing),
        judge_skipped=judge_skipped,
    )


def _worker_prompt(
    goal: str,
    index: int,
    history: tuple[GoalIteration, ...],
    feedback: str,
) -> str:
    response_language = _response_language(goal)
    language_instruction = (
        f"FINAL RESPONSE LANGUAGE: {response_language}. "
        "Write the final user-facing summary in this language, regardless of the "
        "language used by these internal instructions.\n"
    )
    if index == 1:
        return (
            f"{language_instruction}TUJUAN PROYEK: {goal}\n\n"
            "Ini adalah putaran pertama. Folder proyek masih kosong: buat proyek ini DARI AWAL. "
            "Buat hanya file yang benar-benar dibutuhkan untuk mencapai tujuan; jangan membuat "
            "scaffolding, bahasa, atau file tambahan yang tidak diminta. Jika tujuan sudah "
            "menyebut file/perintah verifikasi tertentu, ikuti persis. Sertakan manifest "
            "minimal yang sesuai stack hanya bila diperlukan (mis. pyproject.toml/requirements.txt "
            "untuk Python). Lalu jalankan pemeriksaan relevan dengan run_command. "
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
        "dari kondisi tersebut dan lakukan perubahan minimum. Jangan menambah file yang "
        "tidak diminta.\n\n"
        f"RIWAYAT SINGKAT:\n{summary}\n\n"
        f"YANG MASIH PERLU DIPERBAIKI:\n{feedback or 'Tinjau kembali sisa pekerjaan.'}\n\n"
        "Perbaiki masalah di atas dengan write_file/edit_file, jalankan kembali pemeriksaan "
        "relevan dengan run_command, lalu beri ringkasan singkat."
    )


def _feedback(check: AcceptanceCheck, result: AgentResult) -> str:
    parts: list[str] = []
    if not check.verification_ok:
        failed = [item for item in check.verifications if not item.result.ok]
        parts.append(
            "Perintah verifikasi gagal:\n"
            + "\n".join(
                f"{item.command}\n{_compact_tool_result(item.result.format(), 1200)}"
                for item in failed
            )
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
    use_plan: bool | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> GoalResult:
    """Buat proyek dan jalankan worker per todo kecil sampai tujuan tercapai.

    Secara default goal dipecah menjadi todo sebelum eksekusi. Setiap todo
    menjalankan ``run_agent`` dengan riwayat segar, sehingga prompt tidak
    membengkak. ``llm``/``judge`` yang disuntikkan menonaktifkan planner secara
    otomatis agar test double tetap deterministik; gunakan ``use_plan=True``
    untuk memaksa planner pada pengujian.

    ``run_verify`` dapat diganti untuk keperluan pengujian.
    """
    target_goal = goal.strip()
    if not target_goal:
        raise GoalError("Tujuan tidak boleh kosong.")
    limit = settings.max_goal_iterations if max_iterations is None else max_iterations
    if limit < 1:
        raise GoalError("Batas putaran goal harus lebih besar dari 0.")
    # Batasi juga pemanggil langsung (CLI/service), bukan hanya HTTP.
    limit = min(limit, settings.max_goal_iterations_limit)

    project = resolve_project_dir(settings, target_goal, project_name)
    workspace = settings.workspace_dir.resolve()
    relative = project.relative_to(workspace).as_posix()
    criteria = acceptance or GoalAcceptance()
    verify_fn = run_verify or run_command_in_container
    judge_model = judge or llm
    if judge_model is None:
        judge_model = get_llm(
            settings,
            reasoning=False,
            num_predict=256,
            format="json",
        )
    session_id = uuid.uuid4().hex
    log = _RunLog(settings.logs_dir, session_id, on_event)

    def check_cancelled() -> None:
        if should_cancel is not None and should_cancel():
            raise AgentCancelled("Tugas dihentikan oleh pengguna.")

    check_cancelled()
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

    plan_enabled = use_plan if use_plan is not None else (llm is None and judge is None)
    plan: GoalPlan | None
    if not plan_enabled:
        plan = GoalPlan(todos=[TodoItem(task=target_goal)])
    else:
        # Prioritaskan perencanaan deterministik: gratis, instan, dan cukup bila
        # required_files/nama file sudah jelas dari goal. LLM hanya dipakai bila
        # tidak ada sinyal file yang bisa dipakai.
        plan = _heuristic_plan(target_goal, criteria, limit)
        if plan is not None:
            log.event(
                "goal.plan.created",
                session_id=session_id,
                source="heuristic",
                todos=[item.task for item in plan.todos],
            )
        else:
            plan = plan_goal(
                target_goal,
                project,
                criteria,
                settings,
                judge_model,
                log,
                session_id,
                limit,
            )
    if plan is None or not plan.todos:
        # Planner kustom bisa mengembalikan None/rencana kosong; jangan sampai
        # modulo oleh nol memecah loop sebelum satu pun tugas dijalankan.
        plan = GoalPlan(todos=[TodoItem(task=target_goal)])
    check_cancelled()
    todos = plan.todos[:limit]
    total = len(todos)
    todo_sequence = [todos[(i - 1) % total] for i in range(1, limit + 1)]

    history: tuple[GoalIteration, ...] = ()
    completed: tuple[str, ...] = ()
    feedback = ""
    signature: tuple[str, ...] | None = None
    stuck = 0

    for index, todo in enumerate(todo_sequence, start=1):
        check_cancelled()
        todo_no = ((index - 1) % total) + 1
        prompt = _todo_prompt(target_goal, todo, todo_no, total, completed, feedback)
        log.event(
            "goal.todo.started",
            session_id=session_id,
            iteration=index,
            total_todos=total,
            task=todo.task,
            files=todo.files,
            prompt=prompt,
        )
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
                should_cancel=should_cancel,
                response_language=_response_language(target_goal),
            )
        except AgentCancelled:
            raise
        except AgentError as exc:
            log.event(
                "goal.todo.failed",
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
                f"Todo {index} gagal: {exc}",
            )

        check_cancelled()
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
        check_cancelled()
        history += (GoalIteration(index=index, result=result, acceptance=check),)
        log.event(
            "goal.todo.completed",
            session_id=session_id,
            iteration=index,
            total_todos=total,
            task=todo.task,
            complete=check.complete,
            reason=check.reason,
            worker_response=result.response,
            worker_finished=result.finished,
            files_written=result.files_written,
            steps=result.steps,
        )
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

        completed += (f"{todo.task[:120]} — {'selesai' if result.finished else 'belum'}",)
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
