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
    console.error(`ITSM failed to download project archive ${id}`, error);
    return Response.json(
      { error: "Unable to download the project archive." },
      { status: 502 },
    );
  }
}
