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
    console.error(`ITSM failed to download project file ${id}`, error);
    return Response.json(
      { error: "Unable to download the project file." },
      { status: 502 },
    );
  }
}
