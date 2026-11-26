import { findAll, query } from "@/lib/prometheus";
import { fmtNumber } from "@/lib/format";

export default async function Page() {
  const [up, agentAvail] = await Promise.all([
    query('up'),
    query('avg by (agent_id) (avg_over_time(up{job="jiran-agent"}[1h])) * 100'),
  ]);

  const agents = findAll(up)
    .filter((r) => r.metric.job === "jiran-agent")
    .sort((a, b) =>
      String(a.metric.agent_id).localeCompare(String(b.metric.agent_id)),
    );

  const availability = new Map(
    findAll(agentAvail).map((r) => [r.metric.agent_id ?? "–", r.value ?? null]),
  );

  return (
    <>
      <h2>Agent availability</h2>
      <table>
        <thead>
          <tr>
            <th>Agent ID</th>
            <th>Instance</th>
            <th>Status</th>
            <th>Availability (1h)</th>
          </tr>
        </thead>
        <tbody>
          {agents.map((r) => (
            <tr key={r.metric.instance}>
              <td>{r.metric.agent_id ?? "–"}</td>
              <td>{r.metric.instance}</td>
              <td>{r.value === 1 ? "UP" : "DOWN"}</td>
              <td>{fmtNumber(availability.get(r.metric.agent_id ?? "–"))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
