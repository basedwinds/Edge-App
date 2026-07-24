import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { FuturesTable } from "../components/markets/FuturesTable";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchFutures } from "../api/markets";
import { useFuturesMarkPlaced } from "../hooks/useFuturesMarkPlaced";

export function Futures() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["futures"],
    queryFn: fetchFutures,
  });
  const handleMarkPlaced = useFuturesMarkPlaced("nfl");

  const rows = data ?? [];

  const stats = useMemo(() => {
    const withEdge = rows.filter((r) => r.edge !== null);
    const marketTypes = new Set(rows.map((r) => r.market_type));
    const avgAbsEdge = withEdge.length
      ? withEdge.reduce((sum, r) => sum + Math.abs(r.edge!), 0) / withEdge.length
      : null;
    const maxAbsEdge = withEdge.length ? Math.max(...withEdge.map((r) => Math.abs(r.edge!))) : null;
    return { typeCount: marketTypes.size, rowCount: rows.length, avgAbsEdge, maxAbsEdge };
  }, [rows]);

  return (
    <PageShell title="Futures">
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
            <StatTile label="Market types" value={String(stats.typeCount)} sublabel="division / conference / 1-seed / Super Bowl / playoff" />
            <StatTile label="Markets tracked" value={String(stats.rowCount)} sublabel="per team, per source" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="model vs market, absolute"
            />
            <StatTile
              label="Largest disagreement"
              value={stats.maxAbsEdge !== null ? `${(stats.maxAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="not a validated trading signal"
            />
          </div>
          <FuturesTable rows={rows} onMarkPlaced={handleMarkPlaced} sport="nfl" />
        </>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        Est. % comes from a season Monte Carlo simulation (2,000 trials) using current Elo ratings and
        the real remaining schedule -- not validated to beat the market any more than Elo itself is
        (see Backtests). Playoff seeding uses simplified tiebreakers (record, then head-to-head if
        available, then random), not the NFL's full official tiebreaker rules. Treat "Edge" as the
        size of disagreement between the estimate and the market, not a proven opportunity.
      </p>
    </PageShell>
  );
}
