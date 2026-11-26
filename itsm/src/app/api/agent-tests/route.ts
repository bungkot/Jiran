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
    console.error("ITSM gagal memuat riwayat coding agent", error);
    return Response.json(
      { error: "Riwayat coding agent belum dapat dimuat." },
      { status: 502 },
    );
  }
}

export async function POST(request: Request) {
  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return Response.json({ error: "Body harus berupa JSON." }, { status: 400 });
  }
  const message =
    typeof payload === "object" &&
    payload !== null &&
    "message" in payload &&
    typeof payload.message === "string"
      ? payload.message.trim()
      : "";
  if (!message) {
    return Response.json({ error: "Instruksi pengujian wajib diisi." }, { status: 400 });
  }
  if (message.length > MAX_MESSAGE_LENGTH) {
    return Response.json(
      { error: `Instruksi maksimal ${MAX_MESSAGE_LENGTH} karakter.` },
      { status: 413 },
    );
  }

  try {
    const job = await submitAgentGoal(message);
    return Response.json(job, { status: 202 });
  } catch (error) {
    console.error("ITSM gagal mengirim pengujian ke coding agent", error);
    return Response.json(
      { error: "Coding agent tidak dapat menerima pengujian." },
      { status: 502 },
    );
  }
}
