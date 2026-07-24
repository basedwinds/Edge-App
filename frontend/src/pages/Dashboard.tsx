import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { MarketsTable } from "../components/markets/MarketsTable";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchMarkets, groupByGameAndSource } from "../api/markets";

export function Dashboard() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["markets"],
    queryFn: fetchMarkets,
  });

  const rows = useMemo(() => (data ? groupByGameAndSource(data) : []), [data]);

  const stats = useMemo(() => {
    const withEdge = rows.filter((r) => r.edge !== null);
    const games = new Set(rows.map((r) => r.nfl_game_id));
    const avgAbsEdge = withEdge.length
      ? withEdge.reduce((sum, r) => sum + Math.abs(r.edge!), 0) / withEdge.length
      : null;
    const maxAbsEdge = withEdge.length ? Math.max(...withEdge.map((r) => Math.abs(r.edge!))) : null;
    return { gameCount: games.size, rowCount: rows.length, avgAbsEdge, maxAbsEdge };
  }, [rows]);

  return (
    <PageShell title="Dashboard">
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
            <StatTile label="Games tracked" value={String(stats.gameCount)} sublabel="NFL, matched to Kalshi/Polymarket" />
            <StatTile label="Markets tracked" value={String(stats.rowCount)} sublabel="moneyline, per source" />
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
          <MarketsTable rows={rows} defaultSort={[{ id: "edge", desc: true }]} />
        </>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        Est. % is an Elo baseline, blended with a news/research adjustment when one is on file
        (marked with •). Neither has been shown to beat the market in backtesting (see Backtests)
        — treat "Edge" as the size of disagreement between the estimate and the market, not a
        proven opportunity.
      </p>
    </PageShell>
  );
}
