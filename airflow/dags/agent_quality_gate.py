"""Scheduled smoke tests for the active coding-agent model."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from airflow.operators.python import PythonOperator

from airflow import DAG

sys.path.insert(0, "/opt/airflow")
from benchmark_models import TASKS  # noqa: E402


def _request_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(  # noqa: S310 - URL comes from Compose and fixed API paths
        url, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - URL comes from Compose and fixed API paths
            request, timeout=30
        ) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"{method} {url} returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"{method} {url} returned an invalid JSON object")
    return result


def _sync_incident(*, active: bool, detail: str) -> None:
    base_url = os.environ["ITSM_SYSTEM_URL"].rstrip("/")
    token = os.environ["ITSM_SYSTEM_TOKEN"]
    _request_json(
        "POST",
        f"{base_url}/api/system/incidents",
        token=token,
        payload={
            "fingerprint": "airflow-agent-quality-gate",
            "title": "Scheduled coding-agent quality gate failed",
            "detail": detail[:2000],
            "active": active,
            "severity": "medium",
        },
    )


def run_quality_gate(**context: Any) -> None:
    base_url = os.environ["AGENT_SERVICE_URL"].rstrip("/")
    token = os.environ["AGENT_API_TOKEN"]
    job_timeout = int(os.environ.get("AIRFLOW_AGENT_JOB_TIMEOUT_S", "1800"))
    failures: list[str] = []
    run_id = context["run_id"]
    run_suffix = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:10]

    try:
        _request_json("GET", f"{base_url}/health")
        for index, task in enumerate(TASKS, start=1):
            project = f"airflow-{run_suffix}-{index}"
            submitted = _request_json(
                "POST",
                f"{base_url}/goals",
                token=token,
                payload={
                    "goal": task["goal"],
                    "project": project,
                    "verify": [task["verify"]],
                    "require_files": task["files"],
                    "max_iterations": 3,
                },
            )
            job_id = submitted.get("id")
            if not isinstance(job_id, str) or not job_id:
                raise RuntimeError("Agent API did not return a job id")

            deadline = time.monotonic() + job_timeout
            while True:
                job = _request_json(
                    "GET",
                    f"{base_url}/goals/{job_id}",
                    token=token,
                )
                status = job.get("status")
                if status in {"completed", "failed"}:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Agent job {job_id} exceeded {job_timeout}s")
                time.sleep(5)

            if status != "completed" or not job.get("achieved"):
                failures.append(
                    f"{task['name']}: {job.get('reason') or status or 'not achieved'}"
                )

        if failures:
            raise RuntimeError("Smoke tests failed: " + "; ".join(failures))
    except RuntimeError as exc:
        try:
            _sync_incident(active=True, detail=str(exc))
        except RuntimeError as incident_error:
            raise RuntimeError(
                f"{exc}; ITSM incident sync also failed: {incident_error}"
            ) from exc
        raise

    _sync_incident(active=False, detail="Scheduled coding-agent smoke tests passed.")


with DAG(
    dag_id="agent_quality_gate",
    description="Weekly smoke test of the active model through the agent API",
    start_date=datetime(2026, 1, 5, tzinfo=UTC),
    schedule="0 2 * * 1",
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0},
    tags=["jiran", "quality"],
) as dag:
    PythonOperator(
        task_id="run_agent_smoke_tests",
        python_callable=run_quality_gate,
        execution_timeout=timedelta(hours=2),
    )
