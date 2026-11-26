import { streamAgentJobEvents } from "@/lib/agent";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
  try {
    const upstream = await streamAgentJobEvents(
      id,
      request.signal,
      request.headers.get("Last-Event-ID"),
    );
    if (!upstream.ok || !upstream.body) {
      console.error(
        `ITSM failed to open the task stream ${id}: HTTP ${upstream.status}`,
      );
      return Response.json(
        {
          error:
            upstream.status === 404
              ? "Coding-agent task not found."
              : "Coding-agent stream is unavailable.",
        },
        { status: upstream.status === 404 ? 404 : 502 },
      );
    }

    return new Response(upstream.body, {
      headers: {
        "Cache-Control": "no-cache, no-transform",
        "Content-Type": "text/event-stream; charset=utf-8",
        "X-Accel-Buffering": "no",
      },
    });
  } catch (error) {
    console.error(`ITSM failed to open task stream ${id}`, error);
    return Response.json(
      { error: "Unable to connect to the coding-agent stream." },
      { status: 502 },
    );
  }
}
