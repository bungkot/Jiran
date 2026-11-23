"""CLI for the stack-neutral local coding agent.

Pemakaian harian: kirim saja goal ke layanan standby yang sudah berjalan.

    coding-agent "Buat kalkulator CLI Python"
    coding-agent submit --verify "pytest -q" "Tambahkan tes"   # goal + gerbang verifikasi
    coding-agent job <id>       # status job di layanan standby
    coding-agent serve          # menjalankan layanan (dipakai Compose)

Perintah perawatan tetap tersedia: preflight, ping, tools, container-build,
goal, dan run.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .agent import AgentError, AgentResult, run_agent
from .config import PROJECT_ROOT, Settings, SettingsError
from .container_client import (
    ContainerError,
    ContainerGoalRequest,
    ContainerGoalResult,
    run_container_goal,
)
from .goal import GoalAcceptance, GoalError, GoalResult, run_goal_loop
from .llm import get_llm, ping_llm
from .preflight import check_docker, run_agent_preflight
from .progress import summarize_event
from .schemas import CheckStatus
from .service import serve
from .service_client import ServiceError, fetch_job, submit_goal
from .tools import Sandbox, SandboxError
from .tools.container_runner import run_argv
from .tools.fs_tools import build_fs_tools

console = Console()
_progress_console = Console(stderr=True)
_SUBCOMMANDS = frozenset(
    {
        "preflight",
        "ping",
        "tools",
        "all",
        "goal",
        "container-build",
        "serve",
        "submit",
        "job",
        "run",
        "-h",
        "--help",
    }
)
_ICON = {
    CheckStatus.OK: "[green]✔ OK[/]",
    CheckStatus.WARN: "[yellow]⚠ WARN[/]",
    CheckStatus.FAIL: "[red]✘ FAIL[/]",
}


class _CLIProgress:
    """Render concise live status without exposing prompts or model responses."""

    def __init__(self) -> None:
        self._status: Any | None = None

    def __enter__(self) -> _CLIProgress:
        return self

    def __exit__(self, *_: object) -> None:
        self._stop_status()

    def _stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def _show(self, message: str, *, event: str = "") -> None:
        if event in {
            "model.request",
            "judge.request",
            "verification.started",
            "goal.verification.started",
            "tool.started",
        }:
            if self._status is None:
                self._status = _progress_console.status(Text(message), spinner="dots")
                self._status.start()
            else:
                self._status.update(Text(message))
            return
        self._stop_status()
        _progress_console.print(Text("[agent] ", style="cyan"), end="")
        _progress_console.print(message, markup=False)

    def event(self, event: dict[str, Any]) -> None:
        message = summarize_event(event)
        if message:
            self._show(message, event=str(event.get("event", "")))

    def service_update(self, job: dict[str, Any]) -> None:
        message = job.get("progress")
        status = str(job.get("progress_event", ""))
        if not isinstance(message, str) or not message:
            message = f"Job {job.get('status', 'berjalan')}"
        self._show(message, event=status)

    def output(self, chunk: str) -> None:
        self._stop_status()
        _progress_console.print(chunk, end="", markup=False)


def _reconfigure_stdio_for_windows() -> None:
    """Windows: stdout/stderr default to cp1252, which cannot encode the
    Rich table icons (U+2714/U+2718) — ``coding-agent`` crashed there.
    Reconfigure to UTF-8 so output is printable everywhere. Rich's Console
    reads ``sys.stdout`` lazily, so this works even though ``Console()``
    was created at import time."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def cmd_preflight(settings: Settings) -> bool:
    console.rule("[bold]AGENT PREFLIGHT")
    report = run_agent_preflight(settings)
    table = Table(show_lines=False)
    for col in ("Cek", "Status", "Detail", "Saran"):
        table.add_column(col)
    for c in report.checks:
        table.add_row(c.name, _ICON[c.status], c.detail, c.hint)
    console.print(table)
    console.print("Preflight:", "[green]LULUS[/]" if report.ok else "[red]GAGAL[/]")
    return report.ok


def cmd_ping(settings: Settings) -> bool:
    console.rule("[bold]FASE 1 — Koneksi LLM")
    console.print(f"Model: [cyan]{settings.ollama_model}[/] @ {settings.ollama_base_url}")
    result = ping_llm(get_llm(settings), timeout_s=settings.llm_timeout_s)
    if result.ok:
        console.print(f"Respons: {result.response!r} ({result.latency_s:.2f}s)")
    else:
        console.print(f"[red]Error:[/] {result.error}")
    console.print("GATE Fase 1:", "[green]LULUS[/]" if result.ok else "[red]GAGAL[/]")
    return result.ok


def cmd_tools(settings: Settings) -> bool:
    """Self-test file sandbox and verify Docker daemon readiness."""
    console.rule("[bold]AGENT TOOLS")
    results: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Sandbox(Path(tmp))
        write_file, read_file, list_dir = build_fs_tools(sandbox)

        def check(name: str, cond: bool) -> None:
            results.append((name, cond))

        check(
            "tulis file",
            write_file.invoke({"path": "app/a.txt", "content": "halo"}).startswith("OK"),
        )
        check("baca file", read_file.invoke({"path": "app/a.txt"}) == "halo")
        check("list folder", "a.txt" in list_dir.invoke({"path": "app"}))
        check(
            "tolak ../ traversal",
            write_file.invoke({"path": "../x.txt", "content": "x"}).startswith("ERROR"),
        )
        check("tolak path absolut", read_file.invoke({"path": "/etc/passwd"}).startswith("ERROR"))
        check(
            "tolak node_modules",
            write_file.invoke({"path": "app/node_modules/x.js", "content": "x"}).startswith(
                "ERROR"
            ),
        )

        docker_check = check_docker()
        check("Docker daemon siap", docker_check.ok)

    table = Table()
    table.add_column("Uji")
    table.add_column("Hasil")
    for name, ok in results:
        table.add_row(name, "[green]✔[/]" if ok else "[red]✘[/]")
    console.print(table)
    passed = all(ok for _, ok in results)
    console.print("Tools:", "[green]LULUS[/]" if passed else "[red]GAGAL[/]")
    return passed


def cmd_serve(settings: Settings) -> None:
    console.rule("[bold]AGENT SERVICE")
    console.print(
        f"Standby di http://{settings.service_host}:{settings.service_port} "
        "— satu worker, antrean tanpa batas"
    )
    serve(settings)


def _print_job(job: dict[str, Any]) -> bool:
    achieved = job.get("status") == "completed" and bool(job.get("achieved"))
    console.print(
        f"Job: [cyan]{job.get('id', '?')}[/] | Status: {job.get('status', '?')} | "
        f"Proyek: {job.get('project') or '-'} | "
        f"Putaran: {job.get('iterations', 0)} | File ditulis: {job.get('files_written', 0)}"
    )
    if job.get("status") in {"completed", "failed"}:
        console.print(
            "Tujuan:",
            "[green]TERCAPAI[/]" if achieved else "[red]BELUM TERCAPAI[/]",
            f"— {job.get('reason') or job.get('error') or ''}",
        )
    elif job.get("progress"):
        console.print(f"Progres: {job['progress']}")
    if job.get("response"):
        console.print("\n[bold]Ringkasan[/]")
        console.print(str(job["response"]), markup=False)
    return achieved


def cmd_submit(
    settings: Settings,
    goal: str,
    options: dict[str, Any] | None = None,
    wait: bool = True,
) -> bool:
    # Argumen tambahan hanya diberikan bila dipakai (jalur default = hanya goal).
    kwargs: dict[str, Any] = {}
    if options:
        kwargs["options"] = options
    if not wait:
        kwargs["wait"] = False
    try:
        if wait:
            with _CLIProgress() as progress:
                job = submit_goal(
                    goal, settings, on_progress=progress.service_update, **kwargs
                )
        else:
            job = submit_goal(goal, settings, **kwargs)
    except ServiceError as exc:
        console.print(f"[red]Gagal:[/] {exc}")
        return False
    if not wait:
        console.print(
            f"Goal diterima. Job: [cyan]{job.get('id', '?')}[/] — "
            f"cek dengan: coding-agent job {job.get('id', '?')}"
        )
        return True
    return _print_job(job)


def cmd_job(settings: Settings, job_id: str) -> bool:
    try:
        job = fetch_job(job_id, settings)
    except ServiceError as exc:
        console.print(f"[red]Gagal:[/] {exc}")
        return False
    _print_job(job)
    return job.get("status") != "failed"


def cmd_run(
    settings: Settings,
    prompt: str,
    project: str,
    verification_commands: list[str],
    allow_no_change: bool,
) -> bool:
    console.rule("[bold]CODING AGENT")
    if not _preflight_or_fail(settings):
        return False

    try:
        project_root = Sandbox(settings.workspace_dir).resolve(project)
        if not project_root.is_dir():
            raise SandboxError(f"folder proyek tidak ditemukan: {project!r}")
        with _CLIProgress() as progress:
            result = run_agent(
                prompt,
                project_root,
                settings,
                verification_commands=verification_commands,
                allow_no_changes=allow_no_change,
                on_event=progress.event,
                on_output=progress.output,
            )
    except (AgentError, SandboxError) as exc:
        console.print(f"[red]Gagal:[/] {exc}")
        return False

    _print_agent_result(result)
    return result.ok


def _preflight_or_fail(settings: Settings) -> bool:
    report = run_agent_preflight(settings)
    table = Table()
    for column in ("Cek", "Status", "Detail", "Saran"):
        table.add_column(column)
    for check in report.checks:
        table.add_row(check.name, _ICON[check.status], check.detail, check.hint)
    console.print(table)
    if not report.ok:
        console.print("[red]Preflight agent gagal; tugas tidak dijalankan.[/]")
    return report.ok


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True))


def _goal_payload(result: GoalResult) -> dict[str, Any]:
    return {
        "achieved": result.achieved,
        "reason": result.reason,
        "project": result.project,
        "session_id": result.session_id,
        "iterations": len(result.iterations),
        "steps": result.steps,
        "tool_calls": result.tool_calls,
        "files_written": result.files_written,
        "response": result.response,
    }


def cmd_goal(
    settings: Settings,
    goal: str,
    project: str | None,
    verification_commands: list[str],
    required_files: list[str],
    max_iterations: int | None,
    allow_no_change: bool,
    *,
    container: bool = False,
    json_output: bool = False,
    image: str | None = None,
) -> bool:
    if json_output:
        if not run_agent_preflight(settings).ok:
            _print_json({"achieved": False, "error": "preflight agent gagal"})
            return False
    elif not _preflight_or_fail(settings):
        return False

    if not container:
        if not json_output:
            console.rule("[bold]GOAL LOOP")
        acceptance = GoalAcceptance(
            verification_commands=tuple(verification_commands),
            required_files=tuple(required_files),
        )
        try:
            with _CLIProgress() as progress:
                result = run_goal_loop(
                    goal,
                    settings,
                    project_name=project,
                    acceptance=acceptance,
                    max_iterations=max_iterations,
                    allow_no_changes=allow_no_change,
                    on_event=progress.event,
                    on_output=progress.output,
                )
        except GoalError as exc:
            if json_output:
                _print_json({"achieved": False, "error": str(exc)})
            else:
                console.print(f"[red]Gagal:[/] {exc}")
            return False
        if json_output:
            _print_json(_goal_payload(result))
        else:
            _print_goal_result(result)
        return result.achieved

    if not json_output:
        console.rule("[bold]GOAL LOOP (CONTAINER)")
    request = ContainerGoalRequest(
        goal=goal,
        project=project,
        verification_commands=tuple(verification_commands),
        required_files=tuple(required_files),
        max_iterations=max_iterations,
        allow_no_changes=allow_no_change,
    )
    actual_settings = replace(settings, container_image=image) if image else settings
    try:
        with _CLIProgress() as progress:
            container_result = run_container_goal(
                request, actual_settings, on_output=progress.output
            )
    except ContainerError as exc:
        if json_output:
            _print_json({"achieved": False, "error": str(exc)})
        else:
            console.print(f"[red]Gagal:[/] {exc}")
        return False
    if json_output:
        _print_json(
            container_result.payload
            or {"achieved": False, "error": "output JSON tidak ditemukan"}
        )
    else:
        _print_container_result(container_result)
    return container_result.ok


def _print_container_result(result: ContainerGoalResult) -> None:
    console.print(f"Container: [cyan]{result.container}[/]")
    payload = result.payload or {}
    if payload:
        console.print(
            f"Proyek: {payload.get('project', '?')} | Putaran: {payload.get('iterations', '?')} | "
            f"File ditulis: {payload.get('files_written', '?')}"
        )
        console.print(payload.get("response") or "(tanpa ringkasan)", markup=False)
    console.print(
        "Tujuan:",
        "[green]TERCAPAI[/]" if result.achieved else "[red]BELUM TERCAPAI[/]",
        f"— {payload.get('reason', result.output[-500:])}",
    )
    if not result.ok and result.exit_code is not None and result.exit_code != 0:
        console.print(result.output[-2000:], markup=False)


def cmd_container_build(settings: Settings, image: str | None) -> bool:
    docker = shutil.which("docker")
    if not docker:
        console.print("[red]Docker CLI tidak ditemukan.[/]")
        return False
    target = image or settings.container_image
    console.rule("[bold]BUILD AGENT IMAGE")
    argv = [docker, "build", "-t", target, str(PROJECT_ROOT)]
    result = run_argv(
        argv,
        cwd=PROJECT_ROOT,
        timeout_s=settings.container_goal_timeout_s,
        max_output_chars=max(settings.max_output_chars, 20_000),
    )
    console.print(result.output, markup=False)
    if not result.ok:
        console.print("[red]Build image gagal.[/]")
    return result.ok


def _print_goal_result(result: GoalResult) -> None:
    console.print(f"Session: [cyan]{result.session_id}[/] | Proyek: [cyan]{result.project}[/]")
    console.print(
        f"Putaran: {len(result.iterations)} | Langkah: {result.steps} | "
        f"Tool: {result.tool_calls} | File ditulis: {result.files_written}"
    )
    for item in result.iterations:
        console.print(
            f"  putaran {item.index}: "
            f"{'[green]sesuai[/]' if item.acceptance.complete else '[yellow]belum[/]'}"
        )
    console.print(
        "Tujuan:",
        "[green]TERCAPAI[/]" if result.achieved else "[red]BELUM TERCAPAI[/]",
        f"— {result.reason}",
    )
    if result.response:
        console.print("\n[bold]Ringkasan akhir[/]")
        console.print(result.response, markup=False)
    if result.verifications:
        console.print("\n[bold]Verifikasi terakhir[/]")
        for check in result.verifications:
            console.print(
                f"{check.command}:",
                "[green]LULUS[/]" if check.result.ok else "[red]GAGAL[/]",
            )


def _print_agent_result(result: AgentResult) -> None:
    console.print(f"Session: [cyan]{result.session_id}[/]")
    console.print(f"Langkah model: {result.steps} | Pemanggilan tool: {result.tool_calls}")
    console.print(f"File ditulis: {result.files_written}")
    console.print("\n[bold]Ringkasan agent[/]")
    console.print(result.response or "(agent tidak mengembalikan ringkasan)", markup=False)
    if not result.verifications:
        console.print(
            "\nVerifikasi eksplisit (--verify): [yellow]tidak diminta[/]. "
            "Lihat ringkasan agent untuk pemeriksaan mandiri."
        )
        return
    console.print("\n[bold]Verifikasi[/]")
    for check in result.verifications:
        console.print(
            f"{check.command}:",
            "[green]LULUS[/]" if check.result.ok else "[red]GAGAL[/]",
        )
        if not check.result.ok:
            console.print(check.result.format(), markup=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight", help="Periksa Ollama, Docker, dan workspace")
    subparsers.add_parser("ping", help="Uji koneksi ke Ollama")
    subparsers.add_parser("tools", help="Uji sandbox tools")
    subparsers.add_parser("all", help="Jalankan preflight, ping, dan self-test tools")
    goal_parser = subparsers.add_parser(
        "goal",
        help="Buat proyek dan ulangi agent sampai tujuan tercapai",
    )
    goal_parser.add_argument(
        "--project",
        default=None,
        help="Nama folder proyek (dibuat bila belum ada; default: dari tujuan)",
    )
    goal_parser.add_argument(
        "--verify",
        action="append",
        default=[],
        metavar="COMMAND",
        help="Perintah verifikasi wajib lulus; dapat diulang",
    )
    goal_parser.add_argument(
        "--require-file",
        action="append",
        default=[],
        metavar="PATH",
        help="File yang harus ada sebelum tujuan dianggap tercapai; dapat diulang",
    )
    goal_parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        metavar="N",
        help="Batasi jumlah putaran goal loop",
    )
    goal_parser.add_argument(
        "--allow-no-change",
        action="store_true",
        help="Izinkan putaran tanpa penulisan file (mis. tujuan analisis)",
    )
    goal_parser.add_argument(
        "--container",
        action="store_true",
        help="Jalankan goal loop di dalam image Docker, bukan di host",
    )
    goal_parser.add_argument(
        "--image",
        default=None,
        metavar="IMAGE",
        help="Override image Docker untuk mode --container",
    )
    goal_parser.add_argument(
        "--json",
        action="store_true",
        help="Cetak satu objek JSON sebagai hasil (untuk dipanggil program lain)",
    )
    goal_parser.add_argument("goal", nargs="+", help="Tujuan yang harus dicapai agent")
    build_parser = subparsers.add_parser(
        "container-build", help="Build image Docker agent dari Dockerfile"
    )
    build_parser.add_argument(
        "--image", default=None, metavar="IMAGE", help="Nama/tag image yang dibuat"
    )
    subparsers.add_parser("serve", help="Jalankan layanan goal standby")
    submit_parser = subparsers.add_parser(
        "submit", help="Kirim goal ke layanan standby dengan opsi verifikasi"
    )
    submit_parser.add_argument("--project", default=None, help="Nama folder proyek di workspace")
    submit_parser.add_argument(
        "--verify", action="append", default=[], metavar="COMMAND",
        help="Perintah verifikasi wajib lulus; dapat diulang",
    )
    submit_parser.add_argument(
        "--require-file", action="append", default=[], metavar="PATH",
        help="File yang harus ada sebelum tujuan tercapai; dapat diulang",
    )
    submit_parser.add_argument("--max-iterations", type=int, default=None, metavar="N")
    submit_parser.add_argument("--allow-no-change", action="store_true")
    submit_parser.add_argument(
        "--no-wait", action="store_true", help="Kembali segera dengan id job"
    )
    submit_parser.add_argument("goal", nargs="+", help="Tujuan yang harus dicapai agent")
    job_parser = subparsers.add_parser("job", help="Lihat status satu job di layanan standby")
    job_parser.add_argument("job_id")
    run_parser = subparsers.add_parser(
        "run", help="Jalankan coding agent pada proyek apa pun di WORKSPACE_DIR"
    )
    run_parser.add_argument(
        "--project", default=".", help="Folder proyek relatif terhadap WORKSPACE_DIR"
    )
    run_parser.add_argument(
        "--verify",
        action="append",
        default=[],
        metavar="COMMAND",
        help="Perintah verifikasi dalam Docker; dapat diulang",
    )
    run_parser.add_argument(
        "--allow-no-change",
        action="store_true",
        help="Izinkan hasil tanpa penulisan file (mis. tugas analisis)",
    )
    run_parser.add_argument(
        "prompt", nargs="+", help="Instruksi perubahan atau pekerjaan pada proyek"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio_for_windows()
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()

    if not raw:
        parser.print_help()
        return 1

    if raw[0] not in _SUBCOMMANDS:
        try:
            settings = Settings.load()
        except SettingsError as exc:
            console.print(f"[red]Konfigurasi tidak valid:[/] {exc}")
            return 1
        return 0 if cmd_submit(settings, " ".join(raw)) else 1

    args = parser.parse_args(raw)
    try:
        settings = Settings.load()
    except SettingsError as exc:
        console.print(f"[red]Konfigurasi tidak valid:[/] {exc}")
        return 1

    if args.command == "serve":
        cmd_serve(settings)
        return 0

    if args.command == "submit":
        options: dict[str, Any] = {}
        if args.project:
            options["project"] = args.project
        if args.verify:
            options["verify"] = list(args.verify)
        if args.require_file:
            options["require_files"] = list(args.require_file)
        if args.max_iterations is not None:
            options["max_iterations"] = args.max_iterations
        if args.allow_no_change:
            options["allow_no_changes"] = True
        ok = cmd_submit(settings, " ".join(args.goal), options, wait=not args.no_wait)
        return 0 if ok else 1

    if args.command == "job":
        return 0 if cmd_job(settings, args.job_id) else 1

    if args.command == "goal":
        return (
            0
            if cmd_goal(
                settings,
                " ".join(args.goal),
                args.project,
                args.verify,
                args.require_file,
                args.max_iterations,
                args.allow_no_change,
                container=args.container,
                json_output=args.json,
                image=args.image,
            )
            else 1
        )

    if args.command == "container-build":
        return 0 if cmd_container_build(settings, args.image) else 1

    if args.command == "run":
        return (
            0
            if cmd_run(
                settings,
                " ".join(args.prompt),
                args.project,
                args.verify,
                args.allow_no_change,
            )
            else 1
        )

    steps = {
        "preflight": [cmd_preflight],
        "ping": [cmd_ping],
        "tools": [cmd_tools],
        "all": [cmd_preflight, cmd_ping, cmd_tools],
    }[args.command]
    for step in steps:
        if not step(settings):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
