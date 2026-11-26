export interface AgentAcceptedJob {
  id: string;
  status: string;
}

export interface AgentJobSnapshot {
  id: string;
  status: string;
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

function agentConfig(): { baseUrl: string; token: string } {
  const baseUrl = process.env.AGENT_SERVICE_URL;
  const token = process.env.AGENT_API_TOKEN;
  if (!baseUrl || !token) {
    throw new Error("AGENT_SERVICE_URL dan AGENT_API_TOKEN harus dikonfigurasi.");
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
    throw new Error(`Agent API menolak permintaan: ${detail}`);
  }
  if (!isPayload(payload)) {
    throw new Error("Respons coding agent bukan objek JSON.");
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
      { error: `Unduhan agent gagal: ${detail}` },
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
