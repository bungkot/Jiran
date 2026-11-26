export interface AgentAcceptedJob {
  id: string;
  status: string;
}

export interface AgentJobSummary {
  id: string;
  status: string;
  goal: string;
  created_at: string;
  finished_at?: string | null;
}

export interface AgentJobSnapshot {
  id: string;
  status: string;
  goal?: string;
  created_at?: string;
  progress?: string;
  progress_seq?: number;
  started_at?: string | null;
  finished_at?: string | null;
  response?: string;
  reason?: string;
  error?: string;
  project?: string;
  files_written?: number;
  files?: string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isAcceptedJob(value: unknown): value is AgentAcceptedJob {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    ["queued", "running", "cancelling", "completed", "failed", "cancelled"].includes(
      String(value.status),
    )
  );
}

function isJobSnapshot(value: unknown): value is AgentJobSnapshot {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    ["queued", "running", "cancelling", "completed", "failed", "cancelled"].includes(
      String(value.status),
    ) &&
    (value.goal === undefined || typeof value.goal === "string") &&
    (value.created_at === undefined || typeof value.created_at === "string") &&
    (value.progress === undefined || typeof value.progress === "string") &&
    (value.progress_seq === undefined ||
      typeof value.progress_seq === "number") &&
    (value.started_at === undefined ||
      value.started_at === null ||
      typeof value.started_at === "string") &&
    (value.finished_at === undefined ||
      value.finished_at === null ||
      typeof value.finished_at === "string") &&
    (value.response === undefined || typeof value.response === "string") &&
    (value.reason === undefined || typeof value.reason === "string") &&
    (value.error === undefined || typeof value.error === "string") &&
    (value.project === undefined || typeof value.project === "string") &&
    (value.files_written === undefined ||
      typeof value.files_written === "number") &&
    (value.files === undefined ||
      (Array.isArray(value.files) &&
        value.files.every((file) => typeof file === "string")))
  );
}

function isJobSummary(value: unknown): value is AgentJobSummary {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    ["queued", "running", "cancelling", "completed", "failed", "cancelled"].includes(
      String(value.status),
    ) &&
    typeof value.goal === "string" &&
    typeof value.created_at === "string" &&
    (value.finished_at === undefined ||
      value.finished_at === null ||
      typeof value.finished_at === "string")
  );
}

function isJobList(value: unknown): value is { jobs: AgentJobSummary[] } {
  return (
    isRecord(value) &&
    Array.isArray(value.jobs) &&
    value.jobs.every(isJobSummary)
  );
}

function agentConfig(): { baseUrl: string; token: string } {
  const baseUrl = process.env.AGENT_SERVICE_URL;
  const token = process.env.AGENT_API_TOKEN;
  if (!baseUrl || !token) {
    throw new Error("AGENT_SERVICE_URL and AGENT_API_TOKEN must be configured.");
  }
  return { baseUrl: baseUrl.replace(/\/+$/, ""), token };
}

async function parseResponse<T>(
  response: Response,
  isPayload: (value: unknown) => value is T,
): Promise<T> {
  const payload: unknown = await response.json();
  if (!response.ok) {
    const detail =
      isRecord(payload) &&
      typeof payload.error === "string"
        ? payload.error
        : `HTTP ${response.status}`;
    throw new Error(`The agent API rejected the request: ${detail}`);
  }
  if (!isPayload(payload)) {
    throw new Error("The coding-agent response is not a JSON object.");
  }
  return payload;
}

export async function submitAgentGoal(
  goal: string,
): Promise<AgentAcceptedJob> {
  const { baseUrl, token } = agentConfig();
  const response = await fetch(`${baseUrl}/goals`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ goal }),
    signal: AbortSignal.timeout(10_000),
    cache: "no-store",
  });
  return parseResponse(response, isAcceptedJob);
}

export async function listAgentJobs(): Promise<AgentJobSummary[]> {
  const { baseUrl, token } = agentConfig();
  const response = await fetch(`${baseUrl}/goals`, {
    headers: { Authorization: `Bearer ${token}` },
    signal: AbortSignal.timeout(10_000),
    cache: "no-store",
  });
  const payload = await parseResponse(response, isJobList);
  return payload.jobs
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .map(({ id, status, goal, created_at, finished_at }) => ({
      id,
      status,
      goal,
      created_at,
      finished_at,
    }));
}

export async function getAgentJob(
  jobId: string,
): Promise<AgentJobSnapshot> {
  const { baseUrl, token } = agentConfig();
  const response = await fetch(
    `${baseUrl}/goals/${encodeURIComponent(jobId)}`,
    {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(10_000),
      cache: "no-store",
    },
  );
  return parseResponse(response, isJobSnapshot);
}

export async function cancelAgentJob(
  jobId: string,
): Promise<AgentJobSnapshot> {
  const { baseUrl, token } = agentConfig();
  const response = await fetch(
    `${baseUrl}/goals/${encodeURIComponent(jobId)}`,
    {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(10_000),
      cache: "no-store",
    },
  );
  return parseResponse(response, isJobSnapshot);
}

export async function streamAgentJobEvents(
  jobId: string,
  signal: AbortSignal,
  lastEventId: string | null,
): Promise<Response> {
  const { baseUrl, token } = agentConfig();
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
  };
  if (lastEventId) headers["Last-Event-ID"] = lastEventId;
  return fetch(
    `${baseUrl}/goals/${encodeURIComponent(jobId)}/events`,
    {
      headers,
      signal,
      cache: "no-store",
    },
  );
}

export async function fetchAgentJobArchive(jobId: string): Promise<Response> {
  const { baseUrl, token } = agentConfig();
  return fetch(
    `${baseUrl}/goals/${encodeURIComponent(jobId)}/files.zip`,
    {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(120_000),
      cache: "no-store",
    },
  );
}

export async function fetchAgentJobFile(
  jobId: string,
  filePath: string,
): Promise<Response> {
  const { baseUrl, token } = agentConfig();
  const encodedPath = filePath.split("/").map(encodeURIComponent).join("/");
  return fetch(
    `${baseUrl}/goals/${encodeURIComponent(jobId)}/files/${encodedPath}`,
    {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(120_000),
      cache: "no-store",
    },
  );
}

export async function proxyAgentDownload(upstream: Response): Promise<Response> {
  if (!upstream.ok) {
    const payload: unknown = await upstream.json();
    const detail =
      isRecord(payload) && typeof payload.error === "string"
        ? payload.error
        : `HTTP ${upstream.status}`;
    return Response.json(
      { error: `Agent download failed: ${detail}` },
      { status: upstream.status },
    );
  }
  const headers = new Headers({
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  for (const name of ["Content-Type", "Content-Disposition", "Content-Length"]) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  return new Response(upstream.body, { headers });
}
