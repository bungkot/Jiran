import { cancelAgentJob, getAgentJob } from "@/lib/agent";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
  try {
    const job = await getAgentJob(id);
    return Response.json(job, {
      headers: { "Cache-Control": "no-store" },
    });
  } catch (error) {
    console.error(`ITSM failed to read task status ${id}`, error);
    return Response.json(
      { error: "Unable to retrieve the coding-agent task status." },
      { status: 502 },
    );
  }
}

export async function DELETE(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
  try {
    const job = await cancelAgentJob(id);
    return Response.json(job, {
      headers: { "Cache-Control": "no-store" },
    });
  } catch (error) {
    console.error(`ITSM failed to stop task ${id}`, error);
    return Response.json(
      { error: "Unable to stop the coding-agent task." },
      { status: 502 },
    );
  }
}
