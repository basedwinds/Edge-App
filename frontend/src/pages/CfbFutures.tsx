import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { FuturesTable } from "../components/markets/FuturesTable";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchCfbFutures } from "../api/markets";
import { useFuturesMarkPlaced } from "../hooks/useFuturesMarkPlaced";

export function CfbFutures() {
  const { data, isLoading, isError } = useQuery({ queryKey: ["cfb", "futures"], queryFn: fetchCfbFutures });
  const onMarkPlaced = useFuturesMarkPlaced("cfb");
  const rows = data ?? [];

  const stats = useMemo(() => {
    const withEdge = rows.filter((r) => r.edge !== null);
    const marketTypes = new Set(rows.map((r) => r.market_type));
    const avgAbsEdge = withEdge.length
      ? withEdge.reduce((sum, r) => sum + Math.abs(r.edge!), 0) / withEdge.length
      : null;
    const maxAbsEdge = withEdge.length ? Math.max(...withEdge.map((r) => Math.abs(r.edge!))) : null;
    return { typeCount: marketTypes.size, rowCount: rows.length, avgAbsEdge, maxAbsEdge, withModel: withEdge.length };
  }, [rows]);

  return (
    <PageShell title="College Football Futures">
      {isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}

      {isLoading ? (
        <>
          <StatTilesSkeleton />
          <TableSkeleton />
        </>
      ) : (
        <>
          <div className="flex flex-wrap border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] mb-6 divide-x divide-[var(--color-border)]">
            <StatTile label="Market types" value={String(stats.typeCount)} sublabel="national champion / conference / playoff / win total" />
            <StatTile label="Markets tracked" value={String(stats.rowCount)} sublabel="per team, per source" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel={`across ${stats.withModel} rows with a model estimate`}
            />
            <StatTile
              label="Largest disagreement"
              value={stats.maxAbsEdge !== null ? `${(stats.maxAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="not a validated trading signal"
            />
          </div>
          <FuturesTable rows={rows} onMarkPlaced={onMarkPlaced} sport="cfb" />
        </>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        Priced by the same Monte Carlo season simulation the CFB game model feeds, run forward from
        current Elo ratings with the team-strength sigma fitted for college football (225, notably wider
        than the NFL&rsquo;s 100 &mdash; college talent is far less evenly distributed). Conference and
        playoff markets resolve off simulated final standings.
        Rows whose rating was built largely outside FBS play are shown for tracking only and never
        staked, because a rating earned against non-FBS opposition is not comparable to the field it is
        being priced against. model_validated is false: the sigma was fitted against real season
        outcomes, but no CFB future has yet settled to score the model against the market.
      </p>
    </PageShell>
  );
}
