import { listAgentJobs, submitAgentGoal } from "@/lib/agent";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_MESSAGE_LENGTH = 300;

export async function GET() {
  try {
    const jobs = await listAgentJobs();
    return Response.json(
      { jobs },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch (error) {
    console.error("ITSM failed to load coding-agent history", error);
    return Response.json(
      { error: "Unable to load coding-agent history." },
      { status: 502 },
    );
  }
}

export async function POST(request: Request) {
  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return Response.json({ error: "Request body must be valid JSON." }, { status: 400 });
  }
  const message =
    typeof payload === "object" &&
    payload !== null &&
    "message" in payload &&
    typeof payload.message === "string"
      ? payload.message.trim()
      : "";
  if (!message) {
    return Response.json({ error: "Task instructions are required." }, { status: 400 });
  }
  if (message.length > MAX_MESSAGE_LENGTH) {
    return Response.json(
      { error: `Task instructions must be ${MAX_MESSAGE_LENGTH} characters or fewer.` },
      { status: 413 },
    );
  }

  try {
    const job = await submitAgentGoal(message);
    return Response.json(job, { status: 202 });
  } catch (error) {
    console.error("ITSM failed to submit a task to the coding agent", error);
    return Response.json(
      { error: "The coding agent could not accept the task." },
      { status: 502 },
    );
  }
}
