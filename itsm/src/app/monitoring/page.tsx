import MetricCard from "@/components/MetricCard";
import ServiceCard from "@/components/ServiceCard";
import { findAll, findValue, query } from "@/lib/prometheus";
import { fmtBytes, fmtNumber, fmtPct } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function Page() {
  let results;
  try {
    results = await Promise.all([
      query("up"),
      query(
        'avg by (instance) (1 - sum(rate(node_cpu_seconds_total{mode="idle"}[5m])) / sum(rate(node_cpu_seconds_total[5m]))) * 100',
      ),
      query(
        'avg by (instance) ((node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / node_memory_MemTotal_bytes * 100)',
      ),
      query("avg by (instance) (node_memory_MemTotal_bytes)"),
      query(
        'avg by (instance) ((node_filesystem_size_bytes{mountpoint="/"} - node_filesystem_avail_bytes{mountpoint="/"}) / node_filesystem_size_bytes{mountpoint="/"}) * 100',
      ),
      query("avg(node_load1)"),
      query("avg(ollama_up)"),
      query(
        'avg by (agent_id) (avg_over_time(up{job="jiran-agent"}[1h])) * 100',
      ),
      query("prometheus_ready"),
    ]);
  } catch (error) {
    console.error("ITSM could not retrieve Prometheus metrics", error);
    return (
      <section className="monitoringPage">
        <h1>Monitoring</h1>
        <p className="monitoringError">
          Prometheus is unavailable. Run{" "}
          <code>docker compose --profile monitoring up -d</code> to start the
          metrics dashboard.
        </p>
      </section>
    );
  }
  const [up, cpu, memPct, memTotal, diskPct, load1, ollamaUp, agentAvail, ready] =
    results;

  const availability = new Map(
    findAll(agentAvail).map((result) => [
      result.metric.agent_id ?? "–",
      result.value,
    ]),
  );
  const health = findAll(up)
    .map((result) => ({
      job: result.metric.job ?? "–",
      instance: result.metric.instance ?? "–",
      status: result.value === 1 ? ("UP" as const) : ("DOWN" as const),
    }))
    .sort((a, b) => a.job.localeCompare(b.job));
  const statusDetail = (job: string) => {
    const rows = health.filter((item) => item.job === job);
    if (rows.length === 0) return "No instances found";
    const up = rows.filter((item) => item.status === "UP").length;
    const instanceLabel = rows.length === 1 ? "instance" : "instances";
    return `${up} of ${rows.length} ${instanceLabel} up`;
  };
  const healthStatus = (job: string) =>
    health.some((item) => item.job === job && item.status === "UP")
      ? "UP"
      : "DOWN";

  return (
    <section className="monitoringPage">
      <h1>Service monitoring</h1>

      <section className="nodeMetricsSection" aria-labelledby="node-metrics-title">
        <div className="monitoringSectionHeading">
          <div>
            <p className="eyebrow">HOST OVERVIEW</p>
            <h2 id="node-metrics-title">Node metrics</h2>
            <p className="monitoringSectionDescription">
              Host resource utilization from Node Exporter.
            </p>
          </div>
          <span className="monitoringSourceBadge">NODE EXPORTER</span>
        </div>
        <div className="nodeMetricGrid">
          <MetricCard
            title="CPU usage"
            value={fmtPct(findValue(cpu))}
            subtitle="5-minute average CPU utilization"
          />
          <MetricCard
            title="Memory used"
            value={fmtPct(findValue(memPct))}
            subtitle={`Total: ${fmtBytes(findValue(memTotal))}`}
          />
          <MetricCard
            title="Disk used"
            value={fmtPct(findValue(diskPct))}
            subtitle="Root filesystem utilization"
          />
          <MetricCard
            title="Load average (1 min)"
            value={fmtNumber(findValue(load1))}
            subtitle="Average system load over 1 minute"
          />
        </div>
      </section>

      <h2>Service status</h2>
      <div className="monitoringGrid">
        <ServiceCard
          title="Jiran Agent"
          status={healthStatus("jiran-agent")}
          detail={statusDetail("jiran-agent")}
        />
        <ServiceCard
          title="Node Exporter"
          status={healthStatus("node-exporter")}
          detail={statusDetail("node-exporter")}
        />
        <ServiceCard
          title="Ollama Exporter"
          status={healthStatus("ollama-exporter")}
          detail={statusDetail("ollama-exporter")}
        />
        <ServiceCard
          title="Prometheus Server"
          status={findValue(ready) === 1 ? "UP" : "DOWN"}
          detail={`API: ${findValue(ready)}`}
        />
      </div>

      <h2>Infrastructure services</h2>
      <div className="monitoringGrid">
        <MetricCard
          title="Ollama Exporter"
          value={findValue(ollamaUp) === 1 ? "Available" : "Unavailable"}
          subtitle={`ollama_up: ${fmtNumber(findValue(ollamaUp))}`}
        />
        <MetricCard
          title="Agent (1h)"
          value={fmtPct(availability.get("agent-1"))}
        />
      </div>
    </section>
  );
}
