import MetricCard from "@/components/MetricCard";
import ServiceCard from "@/components/ServiceCard";
import { findAll, findValue, query } from "@/lib/prometheus";
import { fmtBytes, fmtNumber, fmtPct } from "@/lib/format";

export default async function Page() {
  const [up, cpu, memPct, memTotal, diskPct, load1, ollamaUp, agentAvail, ready] =
    await Promise.all([
      query('up'),
      query('avg by (instance) (1 - sum(rate(node_cpu_seconds_total{mode="idle"}[5m])) / sum(rate(node_cpu_seconds_total[5m]))) * 100'),
      query('avg by (instance) ((node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / node_memory_MemTotal_bytes * 100)'),
      query('avg by (instance) (node_memory_MemTotal_bytes)'),
      query('avg by (instance) ((node_filesystem_size_bytes{mountpoint="/"} - node_filesystem_avail_bytes{mountpoint="/"}) / node_filesystem_size_bytes{mountpoint="/"} * 100)'),
      query('avg(node_load1)'),
      query('avg(ollama_up)'),
      query('avg by (agent_id) (avg_over_time(up{job="jiran-agent"}[1h])) * 100'),
      query('prometheus_ready'),
    ]);

  const availability = new Map(
    findAll(agentAvail).map((r) => [r.metric.agent_id ?? "–", r.value]),
  );

  const health = findAll(up)
    .map((r) => ({
      job: r.metric.job ?? "–",
      instance: r.metric.instance ?? "–",
      status: r.value === 1 ? ("UP" as const) : ("DOWN" as const),
    }))
    .sort((a, b) => a.job.localeCompare(b.job));

  const statusDetail = (job: string) => {
    const rows = health.filter((h) => h.job === job);
    if (rows.length === 0) return "belum ditememukan";
    const down = rows.filter((r) => r.status === "DOWN").length;
    return `${rows.length} instance, ${rows.length - down} up`;
  };

  const healthStatus = (job: string) =>
    health.some((h) => h.job === job && h.status === "UP") ? "UP" : "DOWN";

  const prometheusStatus = findValue(ready) === 1 ? "UP" : "DOWN";

  return (
    <>
      <h2>Status Layanan</h2>
      <div className="grid">
        <ServiceCard title="Jiran Agent" status={healthStatus("jiran-agent")} detail={statusDetail("jiran-agent")} />
        <ServiceCard title="Node Exporter" status={healthStatus("node-exporter")} detail={statusDetail("node-exporter")} />
        <ServiceCard title="Ollama Exporter" status={healthStatus("ollama-exporter")} detail={statusDetail("ollama-exporter")} />
        <ServiceCard title="Prometheus Server" status={prometheusStatus} detail={`API: ${findValue(ready)}`} />
      </div>

      <h2>Metrik Node Exporter</h2>
      <div className="grid">
        <MetricCard
          title="Usage CPU"
          value={fmtPct(findValue(cpu))}
          subtitle="Rata-rata rusage CPU 5 menit"
        />
        <MetricCard
          title="Memory Terpakai"
          value={fmtPct(findValue(memPct))}
          subtitle={`Total ${fmtBytes(findValue(memTotal))}`}
        />
        <MetricCard title="Disk / Used" value={fmtPct(findValue(diskPct))} />
        <MetricCard title="Load Average 1m" value={fmtNumber(findValue(load1))} />
      </div>

      <h2>Layanan Peralatan</h2>
      <div className="grid">
        <MetricCard
          title="Ollama Exporter"
          value={findValue(ollamaUp) === 1 ? "Aktif" : "Inaktif"}
          subtitle={`ollama_up: ${fmtNumber(findValue(ollamaUp))}`}
        />
        <MetricCard
          title="Agent 1 (1h)"
          value={fmtPct(availability.get("agent-1"))}
        />
      </div>
    </>
  );
}
