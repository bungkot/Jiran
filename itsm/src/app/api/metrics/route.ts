import type { NextRequest } from "next/server";
import { query } from "@/lib/prometheus";

export async function GET(request: NextRequest) {
  const q = request.nextUrl.searchParams.get("q");
  if (!q) {
    return Response.json(
      { error: "parameter 'q' (PromQL) wajib diisi" },
      { status: 400 },
    );
  }
  try {
    const data = await query(q);
    return Response.json(data);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return Response.json({ error: message }, { status: 502 });
  }
}
