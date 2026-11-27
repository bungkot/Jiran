#!/usr/bin/env python3
"""Watchdog self-healing: deteksi layanan turun, diagnose dengan LLM (Ollama),
lakukan remediasi ringan bila LLM menyarankan restart.

Pemakaian (di container Compose):
    python selfheal.py

Service yang dipantau (harus HTTP 200):
  - ollama   : GET /api/version  (Ollama REST)
  - agent-1  : GET /health       (coding agent)

Prioritas (menurut pemilik stack):
  prio-1 = jiran-agent: diagnosis LLM → restart
  prio-2 = ollama: fallback restart (diagnosis LLM tidak bisa saat LLM itu sendiri down)

Per siklus (interval, default 60s):
  1. Poll semua target layanan dengan timeout 3s.
  2. Prio-1: bila jiran-agent down → minta LLM via Ollama /api/generate dengan
     `format: json` (Ollama memaksa output valid JSON).
  3. LLM mengembalikan {"action": "restart"|"wait"|"alert",
     "target": "<nama service>", "reason": "kalimat pendek"}.
  4. Bila action == restart → restart container via Docker socket (Docker API
     lewat Unix HTTPConnection). Action lain hanya di-log; alert sudah ditangani
     oleh Prometheus alert rules.
  5. Prio-2: bila ollama down → restart fallback (LLM down = tidak bisa diagnose).
  6. Prio-3: sama-sama query sumber daya host (CPU/memori/storage) via
     Prometheus API; bila di atas threshold kritis → diagnose dengan LLM
     ("AI-assisted") dan log keputusannya.

Sumber daya diakses via Prometheus API di http://prometheus:9090 (metrik
node-exporter); lihat deploy/prometheus.yml. Semua keputusan dan tindakan
dicatat ke stdout (docker logs jiran-selfheal-1).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import time
import urllib.parse
import urllib.request
from typing import Any

# Prioritas self-healing menurut pemilik stack:
#   prio-1 = jiran-agent — diagnosis LLM → restart
#   prio-2 = ollama — fallback restart (diagnosis LLM tak bisa saat LLM itu sendiri down)
AGENT_SERVICES = frozenset({"agent-1"})
OLLAMA_SERVICE = "ollama"
DEFAULT_WATCH = tuple(os.environ.get("SELFHEAL_WATCH", "agent-1,ollama").split(","))
COMPOSE_PROJECT = os.environ.get("SELFHEAL_COMPOSE_PROJECT", "jiran")
SOCKET = os.environ.get("SELFHEAL_DOCKER_SOCKET", "/var/run/docker.sock")
TARGET_URLS = {
    "ollama": "http://ollama:11434/api/version",
    "agent-1": "http://agent-1:8765/health",
}

# Keadaan sumber daya host (via Prometheus/node-exporter) — Prio-3.
RESOURCE_QUERIES = {
    "cpu_busy_pct": (
        'sum(rate(node_cpu_seconds_total{mode!="idle"}[5m])) '
        "/ sum(rate(node_cpu_seconds_total[5m])) * 100"
    ),
    "mem_used_pct": "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
    "disk_used_pct": (
        '100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} '
        '/ node_filesystem_size_bytes{mountpoint="/"})'
    ),
}
RESOURCE_THRESHOLDS = {
    "cpu_busy_pct": 85.0,
    "mem_used_pct": 85.0,
    "disk_used_pct": 85.0,
}

# Timeout LLM (Ollama 2B pada CPU bisa jalan lambat; default 120s).
LLM_TIMEOUT_S = float(os.environ.get("SELFHEAL_LLM_TIMEOUT", "240"))

_RESOURCE_PROMPT = """You are the DevOps assistant watching host resources.
You are given the resource usage as `key=value` (persentase).
Propose a small, reversible remediation for the critical state.
Reply ONLY with a valid JSON object:
{"action":"log_only|note|restart_agent","target":"host","reason":"short sentence"}
Rules:
- action "log_only" when only logging the high usage is needed;
- action "note" for an actionable tip, without destructive steps like deleting files;
- action "restart_agent" only when a healthy agent restart may relieve resource pressure.
Use target "host" when there is no specific target."""

_PROMPT = """You are the DevOps assistant of a local stack (Ollama + coding agents).
You are given the current health of services as JSON. Diagnose and propose the smallest remediation.
Reply ONLY with a valid JSON object:
{"action":"restart|wait|alert","target":"<service name>","reason":"<short sentence>"}
Rules:
- Use action "restart" only for the down target from the input, and only when
  a container restart is the natural fix; use it sparingly.
- Use action "wait" when a transient blip is likely, or "alert" when a human action is needed.
- Use the target name from the input exactly."""


def _http_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL harus memakai skema http/https dan host yang valid")
    return url


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection ke alamat UNIX socket (Docker daemon socket)."""

    def __init__(self, sock_path: str) -> None:
        super().__init__("localhost")
        self.sock_path = sock_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[assignment]
        self.sock.connect(self.sock_path)


def check(url: str, timeout_s: float = 3.0) -> bool:
    """True bila GET url mengembalikan HTTP 200. URLError adalah OSError."""
    try:
        url = _http_url(url)
        with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310
            return response.status == 200
    except OSError:
        return False


def poll_state(
    watch: tuple[str, ...],
    timeout_s: float = 3.0,
) -> list[dict[str, str]]:
    state = []
    for name in watch:
        url = TARGET_URLS.get(name, f"http://{name}:1/health")
        state.append({"name": name, "status": "up" if check(url, timeout_s) else "down"})
    return state


def report_incident(
    itsm_url: str,
    token: str,
    fingerprint: str,
    title: str,
    detail: str,
    active: bool,
    severity: str = "medium",
    timeout_s: float = 5.0,
) -> bool:
    if not itsm_url or not token:
        print("[self-heal] ITSM incident reporting belum dikonfigurasi.", flush=True)
        return False
    payload = json.dumps(
        {
            "fingerprint": fingerprint,
            "title": title,
            "detail": detail,
            "active": active,
            "severity": severity,
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310 - URL is validated as HTTP(S) above.
        _http_url(itsm_url.rstrip("/") + "/api/system/incidents"),
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            if not 200 <= response.status < 300:
                print(
                    f"[self-heal] ITSM menolak update insiden {fingerprint}: "
                    f"HTTP {response.status}",
                    flush=True,
                )
                return False
        return True
    except (OSError, ValueError) as error:
        print(
            f"[self-heal] gagal mengirim insiden {fingerprint} ke ITSM: "
            f"{type(error).__name__}: {error}",
            flush=True,
        )
        return False


def find_container_name(sock_path: str, service: str, project: str = COMPOSE_PROJECT) -> str | None:
    """Cari container dari service dan project Compose yang diminta."""
    try:
        conn = UnixHTTPConnection(sock_path)
        filters = {
            "label": [
                f"com.docker.compose.service={service}",
                f"com.docker.compose.project={project}",
            ]
        }
        # all=true: container yang berhenti (kasus yang di-restart) juga harus ditemukan.
        path = "/containers/json?all=true&filters=" + urllib.parse.quote(json.dumps(filters))
        conn.request("GET", path)
        response = conn.getresponse()
        data = json.loads(response.read())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    names = [str(item["Names"][0])[1:] for item in data if item.get("Names")]
    return names[0] if names else None


def docker_restart(sock_path: str, container: str, timeout_s: int = 10) -> bool:
    """Restart container via Docker API; return True saat sukses."""
    try:
        conn = UnixHTTPConnection(sock_path)
        conn.request(
            "POST",
            f"/containers/{container}/restart?t={timeout_s}",
        )
        response = conn.getresponse()
        response.read()
        return 200 <= response.status < 300
    except OSError:
        return False


def ask_llm(
    base_url: str, model: str, down: list[dict[str, str]], timeout_s: float = 0.0
) -> dict[str, Any] | None:
    timeout_s = timeout_s or LLM_TIMEOUT_S
    """Tanya LLM via Ollama /api/generate format json; parse ke dict bila ada."""
    state = json.dumps(down)
    payload = json.dumps(
        {
            "model": model,
            "prompt": _PROMPT + "\n\nServices state: " + state,
            "format": "json",
            "num_predict": 300,
            "stream": False,
        }
    ).encode()
    try:
        request = urllib.request.Request(  # noqa: S310 - validated HTTP(S) service URL
            _http_url(base_url.rstrip("/") + "/api/generate"),
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            body = json.loads(response.read())
        raw = body.get("response")
        if not isinstance(raw, str):
            return None
        decision = json.loads(raw)
        return decision if isinstance(decision, dict) else None
    except (OSError, ValueError):
        return None


def poll_resources(prometheus_url: str, timeout_s: float = 10.0) -> dict[str, float | None]:
    """Query Prometheus API → {key: persentase}; None bila tidak ada data atau error."""
    out: dict[str, float | None] = {}
    for key, query in RESOURCE_QUERIES.items():
        try:
            url = _http_url(
                prometheus_url.rstrip("/") + "/api/v1/query?query=" + urllib.parse.quote(query)
            )
            with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310
                data = json.loads(response.read())
            result = data.get("data", {}).get("result", [])
            out[key] = float(result[0]["value"][1]) if result else None
        except OSError:
            out[key] = None
    return out


def resource_summary(resources: dict[str, float | None]) -> str:
    """Ringkasan singkat: cpu=12.3% mem=45.1% disk=81.4%."""
    return " ".join(
        f"{key}={value:.1f}%" if value is not None else f"{key}=?"
        for key, value in resources.items()
    )


def ask_llm_resources(
    base_url: str, model: str, resources: dict[str, float | None], timeout_s: float = 0.0
) -> dict[str, Any] | None:
    timeout_s = timeout_s or LLM_TIMEOUT_S
    """Diagnosis LLM untuk sumber daya kritis; return None bila tidak ada respons."""
    payload = json.dumps(
        {
            "model": model,
            "prompt": _RESOURCE_PROMPT + "\n\nResources: " + resource_summary(resources),
            "format": "json",
            "num_predict": 300,
            "stream": False,
        }
    ).encode()
    try:
        request = urllib.request.Request(  # noqa: S310 - validated HTTP(S) service URL
            _http_url(base_url.rstrip("/") + "/api/generate"),
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            body = json.loads(response.read())
        raw = body.get("response")
        if not isinstance(raw, str):
            return None
        decision = json.loads(raw)
        return decision if isinstance(decision, dict) else None
    except (OSError, ValueError):
        return None


def _restart_if_target(sock_path: str, target: str, *, project: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"  dry-run: restart {target} diabaikan.", flush=True)
        return True
    container = find_container_name(sock_path, target, project)
    if not container:
        print(f"  container untuk {target} tidak ditemukan; biarkan.", flush=True)
        return False
    if docker_restart(sock_path, container):
        print(f"  [self-heal] restart container {container} (service {target})", flush=True)
        return True
    else:
        print(f"  [self-heal] gagal restart {container}.", flush=True)
        return False


def run_once(
    watch,
    base_url,
    model,
    sock_path,
    prometheus_url,
    *,
    project=COMPOSE_PROJECT,
    dry_run=False,
    itsm_url="",
    itsm_token="",
    strict_reporting=False,
) -> None:
    report_failures = []
    remediation_failures = []
    state = poll_state(watch)
    by_name = {item["name"]: item for item in state}
    for item in state:
        active = item["status"] == "down"
        detail = (
            f"Self-healing detected that service {item['name']} is unhealthy."
            if active
            else f"Service {item['name']} has recovered."
        )
        if not report_incident(
            itsm_url,
            itsm_token,
            f"service:{item['name']}",
            f"Service {item['name']} is down",
            detail,
            active,
            severity="high",
        ):
            report_failures.append(f"service:{item['name']}")

    resources = poll_resources(prometheus_url)
    for key, value in resources.items():
        if value is None:
            continue
        threshold = RESOURCE_THRESHOLDS[key]
        active = value > threshold
        state_text = "above the critical threshold" if active else "back to normal"
        if not report_incident(
            itsm_url,
            itsm_token,
            f"resource:{key}",
            f"Critical resource: {key}",
            f"{key}={value:.1f}% ({state_text}; threshold {threshold:.1f}%).",
            active,
            severity="medium",
        ):
            report_failures.append(f"resource:{key}")
    print(
        f"[{time.strftime('%H:%M:%S')}] resources: {resource_summary(resources)}",
        flush=True,
    )
    down = [item for item in state if item["status"] == "down"]
    if not down:
        print(
            f"[{time.strftime('%H:%M:%S')}] tidak ada service gagal: "
            f"{[item['name'] for item in state]}",
            flush=True,
        )
        if strict_reporting and report_failures:
            raise RuntimeError(
                "ITSM incident reports failed: " + ", ".join(report_failures)
            )
        return
    down_names = [item["name"] for item in down]
    agent_down = [n for n in down_names if n in AGENT_SERVICES]
    ollama_down = [n for n in down_names if n == OLLAMA_SERVICE]
    print(
        f"[{time.strftime('%H:%M:%S')}] DOWN jiran-agent={agent_down or '-'} "
        f"ollama={ollama_down or '-'}",
        flush=True,
    )

    # Prio-1: jiran-agent — diagnosis LLM → restart sesuai keputusan.
    if agent_down:
        decision = ask_llm(base_url, model, [by_name[n] for n in agent_down])
        if decision is None:
            print(f"  LLM tidak merespons diagnosis untuk {agent_down}; biarkan.", flush=True)
        else:
            action = str(decision.get("action", "")).lower()
            target = str(decision.get("target", ""))
            reason = str(decision.get("reason", ""))
            print(
                f"  decision(jiran-agent): action={action} target={target} reason={reason!r}",
                flush=True,
            )
            if action == "restart" and target in agent_down:
                if not _restart_if_target(sock_path, target, project=project, dry_run=dry_run):
                    remediation_failures.append(target)
            elif action != "restart":
                print(f"  action={action}: biarkan (alert rule tetap berjalan).", flush=True)

    # Prio-2: ollama — fallback restart (diagnosis LLM tak bisa saat LLM itu sendiri down).
    for target in ollama_down:
        if not _restart_if_target(sock_path, target, project=project, dry_run=dry_run):
            remediation_failures.append(target)

    # Prio-3: sumber daya kritis → diagnose dengan LLM-assisted (tidak auto-restart).
    critical = {
        key: value
        for key, value in resources.items()
        if value is not None and value > RESOURCE_THRESHOLDS[key]
    }
    if critical:
        print(f"  [resources] kritis: {resource_summary(critical)}", flush=True)
        decision = ask_llm_resources(base_url, model, resources)
        print(f"  [resources] decision={decision}", flush=True)
    if strict_reporting and report_failures:
        raise RuntimeError("ITSM incident reports failed: " + ", ".join(report_failures))
    if strict_reporting and remediation_failures:
        raise RuntimeError("Container restart failed for: " + ", ".join(remediation_failures))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("SELFHEAL_INTERVAL", "60")),
        help="Siklus cek bertama (detik), default 60",
    )
    args = parser.parse_args(argv)
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
    model = os.environ.get("OLLAMA_MODEL", "qwen3.5:2b-q4_K_M")
    sock_path = SOCKET
    prometheus_url = os.environ.get("SELFHEAL_PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
    itsm_url = os.environ.get("SELFHEAL_ITSM_URL", "http://itsm:3000").rstrip("/")
    itsm_token = os.environ.get("ITSM_SYSTEM_TOKEN", "")
    dry_run = os.environ.get("SELFHEAL_DRY_RUN", "").lower() in {"1", "true", "yes"}
    print(
        f"[selfheal] mulai, interval={args.interval}s model={model} "
        f"watch={DEFAULT_WATCH} (jiran-agent prio-1, ollama prio-2, resources prio-3) "
        f"prom={prometheus_url} itsm={itsm_url} sock={sock_path} dry_run={dry_run}",
        flush=True,
    )
    while True:
        try:
            run_once(
                DEFAULT_WATCH,
                base_url,
                model,
                sock_path,
                prometheus_url,
                project=COMPOSE_PROJECT,
                dry_run=dry_run,
                itsm_url=itsm_url,
                itsm_token=itsm_token,
            )
        except Exception as exc:  # noqa: BLE001 - watchdog harus tetap hidup
            print(f"[selfheal] error siklus: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(max(1.0, args.interval))
    return 0


if __name__ == "__main__":
    main()
