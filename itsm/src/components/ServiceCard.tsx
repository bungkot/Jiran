interface Props {
  title: string;
  status: "UP" | "DOWN";
  detail?: string;
}

export default function ServiceCard({ title, status, detail }: Props) {
  return (
    <div className="card">
      <div className="row">
        <h3>{title}</h3>
        <span className={`badge ${status === "UP" ? "up" : "down"}`}>{status}</span>
      </div>
      {detail ? <p className="sub">{detail}</p> : null}
    </div>
  );
}
