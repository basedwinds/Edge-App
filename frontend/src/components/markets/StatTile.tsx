export function StatTile({ label, value, sublabel }: { label: string; value: string; sublabel?: string }) {
  return (
    <div className="px-5 py-3.5 flex-1 min-w-[160px]">
      <div className="text-[10px] uppercase tracking-wide text-[var(--color-text-dim)] mb-1.5">{label}</div>
      <div className="font-mono text-2xl font-semibold text-[var(--color-text)] tabular-nums">{value}</div>
      {sublabel && <div className="text-xs text-[var(--color-text-muted)] mt-1">{sublabel}</div>}
    </div>
  );
}
