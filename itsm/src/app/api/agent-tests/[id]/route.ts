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
    console.error(`ITSM gagal membaca status pengujian ${id}`, error);
    return Response.json(
      { error: "Status pengujian coding agent belum dapat diperbarui." },
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
    console.error(`ITSM gagal menghentikan pengujian ${id}`, error);
    return Response.json(
      { error: "Pengujian coding agent belum dapat dihentikan." },
      { status: 502 },
    );
  }
}
