import {
  fetchAgentJobArchive,
  proxyAgentDownload,
} from "@/lib/agent";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
  try {
    const upstream = await fetchAgentJobArchive(id);
    return proxyAgentDownload(upstream);
  } catch (error) {
    console.error(`ITSM gagal mengunduh arsip proyek ${id}`, error);
    return Response.json(
      { error: "Arsip proyek belum dapat diunduh." },
      { status: 502 },
    );
  }
}
