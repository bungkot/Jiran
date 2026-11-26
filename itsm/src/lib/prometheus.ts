export const PROMETHEUS_URL = process.env.PROMETHEUS_URL || "http://127.0.0.1:9090";

export interface PromResult {
  metric: Record<string, string>;
  value: [number, string];
}

export interface PrometheusResponse {
  status: string;
  resultType?: string;
  result?: PromResult[];
  error?: string;
}

export async function query(q: string): Promise<PrometheusResponse> {
  const url = new URL(`${PROMETHEUS_URL}/api/v1/query`);
  url.searchParams.set("query", q);
  const res = await fetch(url.toString(), { cache: "no-store" });
  const body = (await res.json()) as {
    status?: string;
    data?: PrometheusResponse;
    error?: string;
  };
  if (!res.ok || body.status !== "success" || !body.data) {
    throw new Error(`Prometheus query failed: ${body.error ?? res.status}`);
  }
  return body.data;
}

export function value(v: string | undefined): number | null {
  if (!v) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function findValue(
  result: PrometheusResponse | undefined,
  match?: Record<string, string>,
): number | null {
  const entry = result?.result?.find((r) =>
    Object.entries(match ?? {}).every(([k, val]) => r.metric[k] === val),
  );
  return entry ? value(entry.value[1]) : null;
}

export function findAll(
  result: PrometheusResponse | undefined,
): Array<{ metric: Record<string, string>; value: number | null }> {
  return (result?.result ?? [])
    .map((r) => ({ metric: r.metric, value: value(r.value[1]) }))
    .filter((r): r is { metric: Record<string, string>; value: number } =>
      r.value !== null,
    );
}
