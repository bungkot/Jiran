#!/usr/bin/env python3
"""Autoscaler container coding-agent (jiran-agent-1/2/3).

Tujuan utama (urut):
  - Pertahankan satu container aktif saat tidak ada permintaan.
  - Jalankan worker tambahan hanya bila jumlah job berjalan/menunggu melebihi
    jumlah worker aktif, hingga maksimum AUTOSCALE_MAX (default 3).
  - Hentikan worker idle segera setelah kapasitas tambahannya tidak dibutuhkan.

Aktivitas dilihat dari GET http://<service>:8765/health → {"queued": n, "running": n}.
Tindakan Docker via Docker API lewat Unix socket (tanpa CLI).

Semua keputusan & tindakan dicatat ke stdout (docker logs jiran-autoscale-1).
Env: AUTOSCALE_SERVICES="agent-1,agent-2,agent-3", AUTOSCALE_MAX=3,
     AUTOSCALE_INTERVAL=10, AUTOSCALE_DOCKER_SOCKET=...,
     AUTOSCALE_COMPOSE_PROJECT=jiran, AUTOSCALE_DRAIN_GRACE_SECONDS=60
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

SERVICES = tuple(os.environ.get("AUTOSCALE_SERVICES", "agent-1,agent-2,agent-3").split(","))
SOCKET = os.environ.get("AUTOSCALE_DOCKER_SOCKET", "/var/run/docker.sock")
COMPOSE_PROJECT = os.environ.get("AUTOSCALE_COMPOSE_PROJECT", "jiran")
_HEALTH_URL = "http://{}:8765/health".format


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection ke alamat UNIX socket (Docker daemon socket)."""

    def __init__(self, sock_path: str) -> None:
        super().__init__("localhost")
        self.sock_path = sock_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[assignment]
        self.sock.connect(self.sock_path)


def list_agent_containers() -> dict[str, dict[str, Any]]:
    """Daftar container Compose layanan agent (semua, termasuk stopped)."""
    try:
        conn = UnixHTTPConnection(SOCKET)
        conn.request("GET", "/containers/json?all=true")
        data = json.loads(conn.getresponse().read())
    except (OSError, ValueError):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for item in data:
        labels = item.get("Labels", {})
        service = labels.get("com.docker.compose.service", "")
        if service not in SERVICES or labels.get("com.docker.compose.project") != COMPOSE_PROJECT:
            continue
        names = item.get("Names") or []
        found[service] = {
            "id": item.get("Id", ""),
            "state": item.get("State", ""),
            "name": names[0][1:] if names else service,
        }
    return found


def poll_service(name: str, timeout_s: float = 3.0) -> dict[str, int]:
    """GET /health → {"up": 1, "queued": n, "running": n}; 0 kalau error."""
    try:
        url = _HEALTH_URL(name)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != name or parsed.port != 8765:
            return {"up": 0, "queued": 0, "running": 0}
        with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310
            data = json.loads(response.read())
    except OSError:
        return {"up": 0, "queued": 0, "running": 0}
    return {
        "up": 1,
        "queued": int(data.get("queued", 0) or 0),
        "running": int(data.get("running", 0) or 0),
    }


def container_action(action: str, container_id: str, *, stop_timeout: int = 2) -> bool:
    """POST /containers/{id}/{action}; return True saat status 2xx."""
    path = f"/containers/{container_id}/{action}" + (
        f"?t={stop_timeout}" if action == "stop" else ""
    )
    try:
        conn = UnixHTTPConnection(SOCKET)
        conn.request("POST", path)
        response = conn.getresponse()
        response.read()
        return 200 <= response.status < 300
    except OSError:
        return False


def run_once(
    state: dict[str, Any],
    *,
    max_scale: int,
    drain_grace_s: float = 60.0,
) -> None:
    containers = list_agent_containers()
    polls = {name: poll_service(name) for name in SERVICES}
    now = time.monotonic()
    busy_at: dict[str, float] = state.setdefault("busy_at", {})
    for name, item in polls.items():
        if item["queued"] > 0 or item["running"] > 0:
            busy_at[name] = now

    running_names = sorted(name for name, item in polls.items() if item["up"])
    running_count = len(running_names)
    demand = sum(item["queued"] + item["running"] for item in polls.values())
    busy_names = {name for name, item in polls.items() if item["queued"] > 0 or item["running"] > 0}
    desired_count = min(max_scale, max(1, demand, len(busy_names)))

    if running_count == 0:
        # Jangan biarkan service tanpa worker, termasuk setelah restart manual.
        for name in SERVICES:
            if not polls[name]["up"]:
                cid = containers.get(name, {}).get("id")
                if cid and container_action("start", cid):
                    print(
                        f"[autoscale] {time.strftime('%H:%M:%S')} start {name} "
                        f"(tidak ada yang aktif → mulai {name})",
                        flush=True,
                    )
                break
        return

    # Satu worker cukup untuk satu job aktif; tambah worker hanya untuk beban konkuren.
    if running_count < desired_count:
        for name in SERVICES:
            if not polls[name]["up"]:
                cid = containers.get(name, {}).get("id")
                if cid and container_action("start", cid):
                    print(
                        f"[autoscale] {time.strftime('%H:%M:%S')} start {name} "
                        f"(demand {demand}, target {desired_count})",
                        flush=True,
                    )
                    running_count += 1
                    if running_count >= desired_count:
                        break

    # Jangan hentikan worker yang masih memegang job.
    if running_count > desired_count:
        idle_names = [name for name in reversed(running_names) if name not in busy_names]
        for name in idle_names:
            if running_count <= desired_count:
                break
            last_busy = busy_at.get(name)
            if last_busy is not None and now - last_busy < drain_grace_s:
                continue
            cid = containers.get(name, {}).get("id")
            if cid and container_action("stop", cid):
                print(
                    f"[autoscale] {time.strftime('%H:%M:%S')} stop {name} "
                    f"(demand {demand}, target {desired_count})",
                    flush=True,
                )
                running_count -= 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--interval", type=float, default=float(os.environ.get("AUTOSCALE_INTERVAL", "10"))
    )
    parser.add_argument(
        "--max", type=int, dest="max_scale", default=int(os.environ.get("AUTOSCALE_MAX", "3"))
    )
    args = parser.parse_args(argv)
    if args.max_scale < 1 or args.max_scale > len(SERVICES):
        parser.error(f"--max harus di antara 1 dan {len(SERVICES)}")
    drain_grace_s = float(os.environ.get("AUTOSCALE_DRAIN_GRACE_SECONDS", "60"))
    if drain_grace_s < 0:
        parser.error("AUTOSCALE_DRAIN_GRACE_SECONDS tidak boleh negatif")
    print(
        f"[autoscale] mulai, interval={args.interval:.0f}s max={args.max_scale} "
        f"services={SERVICES} project={COMPOSE_PROJECT} sock={SOCKET}",
        flush=True,
    )
    state: dict[str, Any] = {"busy_at": {}}
    while True:
        try:
            run_once(state, max_scale=args.max_scale, drain_grace_s=drain_grace_s)
        except Exception as exc:  # noqa: BLE001 - autoscaler harus tetap hidup
            print(f"[autoscale] error siklus: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(max(1.0, args.interval))
    return 0


if __name__ == "__main__":
    main()
