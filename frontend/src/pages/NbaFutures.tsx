import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { FuturesTable } from "../components/markets/FuturesTable";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchNbaFutures } from "../api/markets";
import { useFuturesMarkPlaced } from "../hooks/useFuturesMarkPlaced";

export function NbaFutures() {
  const { data, isLoading, isError } = useQuery({ queryKey: ["nba", "futures"], queryFn: fetchNbaFutures });
  const onMarkPlaced = useFuturesMarkPlaced("nba");
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
    <PageShell title="NBA Futures">
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
            <StatTile label="Market types" value={String(stats.typeCount)} sublabel="championship / conference / division / playoff / play-in / record" />
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
          <FuturesTable rows={rows} onMarkPlaced={onMarkPlaced} sport="nba" />
        </>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        Est. % comes from a season Monte Carlo simulation (2,000 trials) using current Elo ratings and the
        real remaining schedule -- not validated to beat the market any more than Elo itself is. Degrades
        to "—" until ESPN publishes the target season's schedule (confirmed not yet published as of this
        build). Playoff seeding uses simplified tiebreakers (win total, then head-to-head if the tied teams
        played in the same trial, then random), not the NBA's real tiebreaker rules. Treat "Edge" as the
        size of disagreement between the estimate and the market, not a proven opportunity.
      </p>
    </PageShell>
  );
}
