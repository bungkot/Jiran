interface Props {
  title: string;
  value: string;
  unit?: string;
  subtitle?: string;
}

export default function MetricCard({ title, value, unit, subtitle }: Props) {
  return (
    <div className="card">
      <h3>{title}</h3>
      <p className="big">
        {value}
        {unit ? <span> {unit}</span> : null}
      </p>
      {subtitle ? <p className="sub">{subtitle}</p> : null}
    </div>
  );
}
