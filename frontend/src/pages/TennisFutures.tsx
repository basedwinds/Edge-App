import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { FuturesTable } from "../components/markets/FuturesTable";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchTennisFutures } from "../api/markets";
import { useFuturesMarkPlaced } from "../hooks/useFuturesMarkPlaced";

export function TennisFutures() {
  const { data, isLoading, isError } = useQuery({ queryKey: ["tennis", "futures"], queryFn: fetchTennisFutures });
  const onMarkPlaced = useFuturesMarkPlaced("tennis");
  const rows = data ?? [];

  const stats = useMemo(() => {
    const withEdge = rows.filter((r) => r.edge !== null);
    const tournaments = new Set(rows.map((r) => r.group_label));
    const avgAbsEdge = withEdge.length
      ? withEdge.reduce((sum, r) => sum + Math.abs(r.edge!), 0) / withEdge.length
      : null;
    return { tournamentCount: tournaments.size, rowCount: rows.length, avgAbsEdge, withModel: withEdge.length };
  }, [rows]);

  return (
    <PageShell title="Tennis Futures">
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
            <StatTile label="Tournaments tracked" value={String(stats.tournamentCount)} sublabel="ATP/WTA tournament-winner markets, Kalshi" />
            <StatTile label="Markets tracked" value={String(stats.rowCount)} sublabel="one row per player in the field" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel={`across ${stats.withModel} rows with a model estimate`}
            />
          </div>
          <FuturesTable rows={rows} onMarkPlaced={onMarkPlaced} sport="tennis" />
        </>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        Model estimate is a bracket Monte Carlo run off the real tournament draw AND real surface
        (tennisexplorer.com) blended into this app's own surface-aware Elo -- starts simulating from the
        deepest round already confirmed by real results, so already-eliminated players correctly show no
        chance rather than a stale pregame guess. A tournament with no published draw yet, or one that's
        already fully decided, shows "—" rather than a guess. model_validated: false, same as everywhere
        else in this app -- not backtested against real historical futures prices (no free source for
        those exists).
      </p>
    </PageShell>
  );
}
