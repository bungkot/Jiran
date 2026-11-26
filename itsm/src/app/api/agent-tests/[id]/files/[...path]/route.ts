import {
  fetchAgentJobFile,
  proxyAgentDownload,
} from "@/lib/agent";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  context: { params: Promise<{ id: string; path: string[] }> },
) {
  const { id, path } = await context.params;
  try {
    const upstream = await fetchAgentJobFile(id, path.join("/"));
    return proxyAgentDownload(upstream);
  } catch (error) {
    console.error(`ITSM gagal mengunduh file proyek ${id}`, error);
    return Response.json(
      { error: "File proyek belum dapat diunduh." },
      { status: 502 },
    );
  }
}
