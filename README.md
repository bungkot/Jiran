# Jiran - Local Coding Agent with Isolated Execution

[![CI](https://github.com/bungkot/Jiran/actions/workflows/ci.yml/badge.svg)](https://github.com/bungkot/Jiran/actions/workflows/ci.yml)

Jiran is a local, stack-neutral coding agent powered by Ollama. It runs
verification commands in disposable Docker containers. The Python package is
named `coding-agent`, and its command-line entry point is also `coding-agent`.

## Architecture

```text
CLI / HTTP client
      |
      v
ITSM (tickets, chat, audit trail)
      |
      v
Single agent service (queue + one worker)
      |
      v
Goal loop -- heuristic planner --> per-file tasks
      |                           (fresh context for each task)
      v
Agent worker (bounded tool calling)
      |
      +-- file tools (sandboxed paths, secret guard, O_NOFOLLOW)
      +-- run_command --> Docker: read-only, cap-drop, no-new-privileges,
                          PID/memory/CPU limits, /tmp noexec, no network
```

The main modules are in `src/agent/`:

| File | Responsibility |
|---|---|
| `config.py` | `Settings` and fail-closed environment validation |
| `preflight.py` | Environment checks for Python, workspace, Docker, Ollama, and model |
| `llm.py` | `ChatOllama` integration and connectivity checks |
| `agent.py` | Tool-calling worker, context compaction, retries, and verification |
| `goal.py` | Goal loop, planner, acceptance checks, judge, and stall detection |
| `service.py` | HTTP server, job queue, and single worker |
| `service_client.py` | Submit and poll jobs |
| `container_client.py` | Run goals in the agent Docker image |
| `tools/` | Sandbox, file and edit tools, and Docker command runner |

## Getting Started

### Run with Ollama on the host

```bash
uv sync --extra dev
export AGENT_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run coding-agent preflight
uv run coding-agent all
uv run coding-agent goal --project demo --verify "python -m unittest discover -q" \
  --require-file calc.py "Create calc.py with add and multiply functions"
```

### Run the full stack with Docker Compose

The canonical Compose configuration is `docker-compose.yaml`. Run these
commands from the repository root:

```bash
export AGENT_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose up -d --build
uv run coding-agent submit "Create a CLI calculator"
uv run coding-agent job <id>
```

`AGENT_API_TOKEN` is **required** by Compose because the service binds to
`0.0.0.0` inside its container, and the `/goals` endpoint can cause the agent
to modify files and execute commands. The token must contain at least 32
characters.

By default, Compose runs one coding agent on port `8765`, bound to the host's
loopback interface. The load balancer and autoscaler are disabled. Jobs are
processed by a single worker and serialized through the same queue. Host-side
CLI commands use `AGENT_SERVICE_URL=http://localhost:8765`; commands running
inside a container use `http://agent-1:8765`. Jobs and the queue are held in
memory and are lost when the container restarts. Jobs targeting the same
project are serialized using a shared file lock. Additional worker definitions,
Nginx, and the autoscaler remain available in the `legacy-scaling` profile but
are not started by default.

### ITSM and the coding-agent playground

Compose also starts the ITSM Next.js application at `http://localhost:3000`.
The home page separates automatically created incidents from the coding-agent
playground. Self-healing creates or updates a ticket when a service fails or a
resource metric exceeds its threshold, then marks the ticket as recovered when
the condition clears. Service outages are high severity; resource threshold
alerts are medium severity. Low, medium, and high severities are shown in
green, yellow, and red, respectively. Older tickets without a severity are
displayed as medium.

Messages in the playground submit jobs directly to the coding agent and do not
create tickets. Tickets are stored in the `itsm_data` Docker volume. Playground
chat history lasts only while the page remains open. Agent progress is streamed
to the chat using Server-Sent Events (SSE), and the final response appears when
the job completes.

Set `ITSM_SYSTEM_TOKEN` to use a dedicated token for self-healing reports. If
it is empty, Compose uses `AGENT_API_TOKEN`.

The ITSM port binds to the host's loopback interface (`127.0.0.1`) by default.
Do not expose it publicly until user authentication and HTTPS are configured:
chat access can cause the coding agent to modify files and run commands. The
agent token is used only by the ITSM server and is not sent to the browser. Set
`ITSM_PORT` to change the port.

The Ollama API is published only on the host's loopback interface at
`OLLAMA_PORT` (default `11434`). This allows local CLI commands and benchmarks
to access it without exposing it to the network.

The default Ollama image is `ollama/ollama:latest`. For Docker installations
with limited storage or hosts without a GPU, set
`OLLAMA_IMAGE=ollama/ollama:rocm` to use a smaller image; the CPU backend
remains available.

### Optional Prometheus and Grafana monitoring

Start monitoring only when needed:

```bash
docker compose --profile monitoring up -d
```

Grafana is available at `http://localhost:3001` with a preconfigured Jiran
dashboard. Prometheus is available at `http://localhost:9090`. Both ports bind
only to the host's loopback interface. The Grafana dashboard is available
without login and is read-only. Prometheus retains data for seven days.

Prometheus collects queue, completed-job, and duration metrics from the
single-agent service, as well as host CPU, memory, and filesystem metrics from
Node Exporter. The `/metrics` endpoint is available only on the internal
Compose network. Node Exporter requires read-only mounts of `/`, `/proc`, and
`/sys` to read host metrics. The monitoring containers have a combined memory
limit of 896 MiB. Stop them when not needed:

```bash
docker compose stop grafana prometheus node-exporter
```

## CLI Commands

| Command | Description |
|---|---|
| `preflight`, `ping`, `tools`, `all` | Check the environment |
| `goal ...` | Run the local goal loop |
| `goal --container ...` | Run the goal loop in a Docker image |
| `goal --json ...` | Produce JSON output for automation and benchmarks |
| `run --project ...` | Work on an existing project in the workspace |
| `serve` | Start the standby service |
| `submit ... [--no-wait]` | Submit a goal to the service |
| `job <id>` | Check a job's status |
| `container-build` | Build the agent image |

If the first argument is not a subcommand, it is treated as a goal and
submitted to the standby service:

```bash
coding-agent "Create a CLI calculator"
```

## Security Model

- **Files:** All paths are validated against the workspace. Path traversal,
  absolute paths, null bytes, and symlink escapes are rejected. Sensitive
  files (such as `.env*`, credentials, keys, and `secrets.*`) cannot be read or
  written. Writes use `O_NOFOLLOW` and inode checks to prevent TOCTOU races.
- **Commands:** Commands run in containers with `--read-only`,
  `--cap-drop=ALL`, `--security-opt=no-new-privileges`, PID/memory/CPU limits,
  a `/tmp` tmpfs mounted with `noexec,nodev,nosuid`, and networking disabled.
  Sensitive files are masked with `/dev/null` or tmpfs. Execution is rejected
  if a symlink points outside the workspace.
- **Service:** The service binds to loopback by default. Non-loopback binding
  without a token is rejected. Bearer tokens are case-insensitive,
  `max_iterations` is limited to 1-50, and the queue holds at most 100 jobs
  (additional submissions receive HTTP 429). Internal errors are not exposed
  through the API.
- **Logs:** Files under `logs/run-*.jsonl` have `0600` permissions. Job
  metadata does not store the goal or response. Preview old logs before
  removing them:

  ```bash
  python scripts/cleanup_logs.py --days 14 --dry-run
  ```

### Known limitations

- `run_command` provides an unrestricted shell **inside the container**, with
  the workspace mounted read-write. Prompt injection in repository contents
  can still modify project files.
- Compose mounts the Docker socket because verification runs in nested
  containers. Access to the Docker daemon is equivalent to host root access.
  Do not expose `/goals` without a token or run the service against untrusted
  repositories.
- `AGENT_CONTAINER_DOCKER_SOCKET` (default `false`) applies specifically to
  `goal --container` mode. Enabling it gives the goal container access to the
  host Docker daemon.
- `--storage-opt size=` limits the container's writable layer, **not** the
  `/workspace` bind mount. To help prevent the host disk from filling up, set
  `AGENT_SANDBOX_MIN_FREE_DISK_MB`.

## Configuration

See `.env.example` for the complete list. Commonly adjusted settings:

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_IMAGE` | `ollama/ollama:latest` | Ollama runtime image; `ollama/ollama:rocm` is smaller for storage-constrained hosts |
| `OLLAMA_PORT` | `11434` | Ollama API port on the host; binds only to `127.0.0.1` |
| `OLLAMA_MODEL` | `qwen3.5:2b-q4_K_M` | Default local model; the 4B variant is more capable but larger |
| `OLLAMA_NUM_CTX` | `8192` | Adjust based on available host memory |
| `MAX_CONTEXT_CHARS` | `12000` | Total message-history budget |
| `MAX_AGENT_STEPS` / `MAX_AGENT_TOOL_CALLS` | `15` / `30` | Worker limits |
| `MAX_GOAL_ITER` / `MAX_GOAL_ITER_LIMIT` | `5` / `50` | Goal-loop limits |
| `REQUIRE_JUDGE_ON_FAST_PATH` | `false` | Set to `true` to call the judge even when machine verification passes |
| `AGENT_SANDBOX_NETWORK` | `none` | Use `bridge` only when network access is required |
| `AGENT_SANDBOX_MEMORY_MB` / `AGENT_SANDBOX_CPUS` | `256` / `1` (Compose) | Per-sandbox limits; increase only when sufficient memory is available |
| `AGENT_API_TOKEN` | Unset | Required when binding to a non-loopback interface |
| `AGENT_SERVICE_PORT` | `8765` | Agent service port on the host; binds to `127.0.0.1` |
| `AGENT_SANDBOX_MIN_FREE_DISK_MB` | `256` | Reject writes when disk space is low |
| `AGENT_SANDBOX_DISK_QUOTA_MB` | `0` | Container writable-layer quota |

## Development and CI

Install development dependencies and run the local checks:

```bash
uv sync --extra dev
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy src/agent
uv run pytest -q --cov
```

CI runs the checks on Python 3.13. Integration tests
(`RUN_OLLAMA=1`, `RUN_AGENT_E2E=1`) require a real Ollama instance and Docker;
they are run manually through `workflow_dispatch`.

## Model Benchmarks

```bash
python scripts/benchmark_models.py qwen3.5:2b-q4_K_M
```

The script exits with status 1 if any task or model fails, so it can be used as
a regression gate. By default, it uses `docker exec` to run inside the `agent`
container (or `jiran` for older deployments). Set `BENCH_AGENT_CONTAINER` to
use a custom container name, or leave it unset to fall back to running
`python -m agent.main` on the host.
