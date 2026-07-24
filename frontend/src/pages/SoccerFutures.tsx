import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { FuturesTable } from "../components/markets/FuturesTable";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchSoccerFutures } from "../api/markets";
import { useFuturesMarkPlaced } from "../hooks/useFuturesMarkPlaced";

export function SoccerFutures() {
  const { data, isLoading, isError } = useQuery({ queryKey: ["soccer", "futures"], queryFn: fetchSoccerFutures });
  const onMarkPlaced = useFuturesMarkPlaced("soccer");
  const rows = data ?? [];

  const stats = useMemo(() => {
    const withEdge = rows.filter((r) => r.edge !== null);
    const leagues = new Set(rows.map((r) => r.group_label));
    const avgAbsEdge = withEdge.length
      ? withEdge.reduce((sum, r) => sum + Math.abs(r.edge!), 0) / withEdge.length
      : null;
    return { leagueCount: leagues.size, rowCount: rows.length, avgAbsEdge, withModel: withEdge.length };
  }, [rows]);

  return (
    <PageShell title="Soccer Futures">
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
            <StatTile label="Leagues tracked" value={String(stats.leagueCount)} sublabel="EPL/La Liga/Serie A/Bundesliga/Ligue 1 champion + relegation markets, Kalshi" />
            <StatTile label="Markets tracked" value={String(stats.rowCount)} sublabel="one row per team in the field, per market" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel={`across ${stats.withModel} rows with a model estimate`}
            />
          </div>
          <FuturesTable rows={rows} onMarkPlaced={onMarkPlaced} sport="soccer" />
        </>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        League Winner and Relegation (automatic drop-zone: bottom 3 for EPL/La Liga/Serie A, bottom 2 for
        Bundesliga/Ligue 1 -- the latter two also use a real relegation PLAYOFF round for the team just above
        that zone, which this model does not simulate, so its relegation estimate is a lower bound for the team
        right at that boundary). Top-4/Champions-League-qualification and MLS Cup aren't built (Kalshi's own
        Top-4 markets had zero real open events as of this build; MLS Cup is a single-elimination playoff, a
        different structure this round-robin model doesn't cover). Model estimate is a Monte Carlo double
        round-robin (every team plays every other team home and away, the REAL fixture structure all 5 of these
        leagues actually use) off this app's own attack/defense Poisson ratings -- a preseason snapshot, not a
        walk-forward-updated in-season simulation, and not separately backtested against real historical
        futures prices (no free source for those exists; only the underlying match-level model has a real
        backtest, see Backtests). A newly-promoted team gets a real rating derived from their own final
        second-tier form (shifted by a data-derived promotion discount, see season_sim_soccer.py), falling back
        to a rough bottom-quartile placeholder only if no second-tier history exists either.
        model_validated: false, same as everywhere else in this app.
      </p>
    </PageShell>
  );
}
