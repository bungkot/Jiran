import { submitAgentGoal } from "@/lib/agent";
import {
  addTicketMessage,
  addTicketTransaction,
  getTicket,
  updateTicket,
} from "@/lib/tickets";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_MESSAGE_LENGTH = 4000;

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
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
    return Response.json({ error: "Pesan wajib diisi." }, { status: 400 });
  }
  if (message.length > MAX_MESSAGE_LENGTH) {
    return Response.json(
      { error: `Pesan maksimal ${MAX_MESSAGE_LENGTH} karakter.` },
      { status: 413 },
    );
  }

  const existing = await getTicket(id);
  if (!existing) {
    return Response.json({ error: "Tiket tidak ditemukan." }, { status: 404 });
  }
  const contextMessages = existing.messages
    .filter((item) => item.role !== "system")
    .slice(-4)
    .map((item) => `${item.role}: ${item.content.slice(0, 1000)}`)
    .join("\n");
  const goal = [
    `Tiket ${existing.id}: ${existing.title}`,
    "Konteks percakapan sebelumnya:",
    contextMessages || "(tidak ada)",
    "Permintaan terbaru:",
    message,
  ].join("\n\n");

  await updateTicket(id, (ticket) => {
    addTicketMessage(ticket, "user", message);
    ticket.status = "in_progress";
    addTicketTransaction(ticket, "Pesan ditambahkan", "Permintaan lanjutan dicatat.");
  });
  try {
    const accepted = await submitAgentGoal(goal);
    if (!accepted.id || !accepted.status) {
      throw new Error("Respons coding agent tidak memiliki id/status job.");
    }
    const updated = await updateTicket(id, (ticket) => {
      ticket.jobs.push({
        id: accepted.id,
        status: accepted.status,
        progress: "Permintaan masuk antrean coding agent.",
        progressSeq: 0,
        createdAt: new Date().toISOString(),
      });
      addTicketTransaction(
        ticket,
        "Dikirim ke coding agent",
        `Job ${accepted.id} diterima dengan status ${accepted.status}.`,
      );
    });
    return Response.json(updated, { status: 202 });
  } catch (error) {
    console.error(`ITSM gagal mengirim pesan tiket ${id} ke coding agent`, error);
    const updated = await updateTicket(id, (ticket) => {
      ticket.status = "failed";
      addTicketMessage(
        ticket,
        "system",
        "Coding agent tidak dapat menerima pesan. Periksa koneksi layanan lalu coba lagi.",
      );
      addTicketTransaction(
        ticket,
        "Pengiriman gagal",
        "Coding agent tidak menerima job; percakapan tetap tersimpan.",
      );
    });
    return Response.json(
      {
        ticket: updated,
        error: "Coding agent tidak dapat menerima permintaan.",
      },
      { status: 502 },
    );
  }
}
