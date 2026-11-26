import { listTickets } from "@/lib/tickets";

export const dynamic = "force-dynamic";

export async function GET() {
  return Response.json(await listTickets(), {
    headers: { "Cache-Control": "no-store" },
  });
}
