"""Monitor Jiran services and resources, report incidents, and remediate failures."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import selfheal
from airflow.operators.python import PythonOperator

from airflow import DAG


def monitor_and_heal() -> None:
    watch = tuple(
        name.strip()
        for name in os.environ.get("SELFHEAL_WATCH", "agent-1,ollama").split(",")
        if name.strip()
    )
    selfheal.run_once(
        watch,
        os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/"),
        os.environ.get("OLLAMA_MODEL", "qwen3.5:2b-q4_K_M"),
        os.environ.get("SELFHEAL_DOCKER_SOCKET", "/var/run/docker.sock"),
        os.environ.get("SELFHEAL_PROMETHEUS_URL", "http://prometheus:9090").rstrip("/"),
        project=os.environ.get("SELFHEAL_COMPOSE_PROJECT", "jiran"),
        dry_run=os.environ.get("SELFHEAL_DRY_RUN", "").lower() in {"1", "true", "yes"},
        itsm_url=os.environ.get("ITSM_SYSTEM_URL", "http://itsm:3000").rstrip("/"),
        itsm_token=os.environ.get("ITSM_SYSTEM_TOKEN", ""),
        strict_reporting=True,
    )


with DAG(
    dag_id="jiran_self_healing",
    description="Check service and host health, synchronize ITSM incidents, and remediate failures",
    start_date=datetime(2026, 1, 5, tzinfo=UTC),
    schedule="*/1 * * * *",
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0},
    tags=["jiran", "self-healing"],
) as dag:
    PythonOperator(
        task_id="monitor_and_heal",
        python_callable=monitor_and_heal,
        execution_timeout=timedelta(minutes=5),
    )
