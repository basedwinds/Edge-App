export function ProbabilityBar({
  label,
  value,
  color,
  sublabel,
}: {
  label: string;
  value: number | null;
  color: string;
  sublabel?: string;
}) {
  const pct = value !== null ? value * 100 : 0;
  return (
    <div>
      <div className="flex items-baseline justify-between mb-1.5">
        <span className="text-sm text-[var(--color-text-dim)]">{label}</span>
        <span className="text-lg font-semibold tabular-nums">{value !== null ? `${pct.toFixed(1)}%` : "—"}</span>
      </div>
      <div className="h-2.5 rounded-full bg-white/5 overflow-hidden">
        <div
          className="h-full rounded-full transition-all"
          style={{ width: `${pct}%`, backgroundColor: color }}
        />
      </div>
      {sublabel && <div className="text-xs text-[var(--color-text-muted)] mt-1">{sublabel}</div>}
    </div>
  );
}
