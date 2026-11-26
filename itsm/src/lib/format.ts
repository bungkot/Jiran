export function fmtPct(n: number | null | undefined): string {
  if (n === null || n === undefined) return "–";
  return `${Math.round(n)}%`;
}

export function fmtNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return "–";
  return String(Math.round(n * 10) / 10);
}

export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "–";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${i === 0 ? Math.round(v) : Math.round(v * 10) / 10} ${units[i]}`;
}
