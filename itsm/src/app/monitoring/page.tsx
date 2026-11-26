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
    console.error("ITSM monitoring tidak dapat mengambil metrik Prometheus", error);
    return (
      <section className="monitoringPage">
        <h1>Monitoring</h1>
        <p className="monitoringError">
          Prometheus belum tersedia. Jalankan{" "}
          <code>docker compose --profile monitoring up -d</code> untuk membuka
          dashboard metrik.
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
    if (rows.length === 0) return "belum ditemukan";
    const down = rows.filter((item) => item.status === "DOWN").length;
    return `${rows.length} instance, ${rows.length - down} up`;
  };
  const healthStatus = (job: string) =>
    health.some((item) => item.job === job && item.status === "UP")
      ? "UP"
      : "DOWN";

  return (
    <section className="monitoringPage">
      <h1>Monitoring layanan</h1>

      <section className="nodeMetricsSection" aria-labelledby="node-metrics-title">
        <div className="monitoringSectionHeading">
          <div>
            <p className="eyebrow">HOST OVERVIEW</p>
            <h2 id="node-metrics-title">Metrik node</h2>
            <p className="monitoringSectionDescription">
              Ringkasan penggunaan resource host dari Node Exporter.
            </p>
          </div>
          <span className="monitoringSourceBadge">NODE EXPORTER</span>
        </div>
        <div className="nodeMetricGrid">
          <MetricCard
            title="Usage CPU"
            value={fmtPct(findValue(cpu))}
            subtitle="Rata-rata penggunaan CPU 5 menit"
          />
          <MetricCard
            title="Memori terpakai"
            value={fmtPct(findValue(memPct))}
            subtitle={`Total ${fmtBytes(findValue(memTotal))}`}
          />
          <MetricCard
            title="Disk / Used"
            value={fmtPct(findValue(diskPct))}
            subtitle="Filesystem root terpakai"
          />
          <MetricCard
            title="Load Average 1m"
            value={fmtNumber(findValue(load1))}
            subtitle="Rata-rata beban selama 1 menit"
          />
        </div>
      </section>

      <h2>Status layanan</h2>
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

      <h2>Layanan peralatan</h2>
      <div className="monitoringGrid">
        <MetricCard
          title="Ollama Exporter"
          value={findValue(ollamaUp) === 1 ? "Aktif" : "Inaktif"}
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
