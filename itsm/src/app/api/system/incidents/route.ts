import { timingSafeEqual } from "node:crypto";
import { syncSystemIncident, type TicketSeverity } from "@/lib/tickets";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function isAuthorized(request: Request): boolean {
  const expected = process.env.ITSM_SYSTEM_TOKEN;
  const provided = request.headers.get("authorization")?.replace(/^Bearer /, "");
  if (!expected || !provided) return false;
  const expectedBytes = Buffer.from(expected);
  const providedBytes = Buffer.from(provided);
  return (
    expectedBytes.length === providedBytes.length &&
    timingSafeEqual(expectedBytes, providedBytes)
  );
}

function isText(value: unknown, maxLength: number): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= maxLength;
}

function isSeverity(value: unknown): value is TicketSeverity {
  return value === "low" || value === "medium" || value === "high";
}

export async function POST(request: Request) {
  if (!process.env.ITSM_SYSTEM_TOKEN) {
    return Response.json(
      { error: "Token integrasi self-healing belum dikonfigurasi." },
      { status: 503 },
    );
  }
  if (!isAuthorized(request)) {
    return Response.json({ error: "Tidak diizinkan." }, { status: 401 });
  }

  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return Response.json({ error: "Body harus berupa JSON." }, { status: 400 });
  }
  if (typeof payload !== "object" || payload === null) {
    return Response.json({ error: "Data insiden tidak valid." }, { status: 400 });
  }
  const severity = "severity" in payload ? payload.severity : undefined;
  if (
    !("fingerprint" in payload) ||
    !isText(payload.fingerprint, 128) ||
    !("title" in payload) ||
    !isText(payload.title, 160) ||
    !("detail" in payload) ||
    !isText(payload.detail, 2000) ||
    !("active" in payload) ||
    typeof payload.active !== "boolean" ||
    (severity !== undefined && !isSeverity(severity))
  ) {
    return Response.json({ error: "Data insiden tidak valid." }, { status: 400 });
  }

  try {
    const ticket = await syncSystemIncident({
      fingerprint: payload.fingerprint.trim(),
      title: payload.title.trim(),
      detail: payload.detail.trim(),
      active: payload.active,
      severity,
    });
    return Response.json(
      { ticket },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch (error) {
    console.error("ITSM gagal menyinkronkan insiden self-healing", error);
    return Response.json(
      { error: "Insiden self-healing gagal disimpan." },
      { status: 500 },
    );
  }
}
