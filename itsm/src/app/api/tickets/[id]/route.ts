import { getTicket } from "@/lib/tickets";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
  const ticket = await getTicket(id);
  if (!ticket) {
    return Response.json({ error: "Tiket tidak ditemukan." }, { status: 404 });
  }
  return Response.json(ticket, {
    headers: { "Cache-Control": "no-store" },
  });
}
