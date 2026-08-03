import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { useParams } from "react-router-dom";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { PlacedBetsTable } from "../components/markets/PlacedBetsTable";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchPlacedBets, settlePlacedBet, deletePlacedBet } from "../api/markets";
import { isRacingSeries } from "../lib/sports";

const SERIES_LABEL: Record<string, string> = { f1: "Formula 1", irl: "IndyCar", nascar: "NASCAR" };

export function RacingPlaced() {
  const { series: seriesParam } = useParams<{ series?: string }>();
  // Narrowed, not cast: the param is whatever is in the URL.
  const series = isRacingSeries(seriesParam) ? seriesParam : "f1";
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: ["placed-bets", series],
    queryFn: () => fetchPlacedBets(undefined, series),
  });

  const rows = data ?? [];
  const pending = useMemo(() => rows.filter((r) => r.status === "pending"), [rows]);
  const settled = useMemo(() => rows.filter((r) => r.status !== "pending"), [rows]);

  const stats = useMemo(() => {
    const lockedUnits = pending.reduce((sum, r) => sum + (r.stake_units ?? 0), 0);
    const wins = settled.filter((r) => r.status === "won").length;
    const losses = settled.filter((r) => r.status === "lost").length;
    const netUnits = settled.reduce((sum, r) => {
      if (r.status === "won" && r.market_prob_at_placement) {
        const price = r.market_prob_at_placement;
        return sum + (r.stake_units ?? 0) * ((1 - price) / price);
      }
      if (r.status === "lost") return sum - (r.stake_units ?? 0);
      return sum;
    }, 0);
    return { lockedUnits, wins, losses, netUnits };
  }, [pending, settled]);

  async function handleSettle(id: number, status: "won" | "lost" | "push" | "void") {
    await settlePlacedBet(id, status);
    queryClient.invalidateQueries({ queryKey: ["placed-bets", series] });
    queryClient.invalidateQueries({ queryKey: ["racing", "markets"] });
  }
  async function handleDelete(id: number) {
    await deletePlacedBet(id);
    queryClient.invalidateQueries({ queryKey: ["placed-bets", series] });
    queryClient.invalidateQueries({ queryKey: ["racing", "markets"] });
  }

  return (
    <PageShell title={`${SERIES_LABEL[series] ?? series.toUpperCase()} — Placed Bets`}>
      {isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}
      {isLoading ? (
        <>
          <StatTilesSkeleton count={3} />
          <TableSkeleton cols={7} />
        </>
      ) : (
        <>
          <div className="flex flex-wrap border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] mb-6 divide-x divide-[var(--color-border)]">
            <StatTile label="Pending" value={String(pending.length)} sublabel={`${stats.lockedUnits.toFixed(1)}u locked up`} />
            <StatTile label="Won / Lost" value={`${stats.wins} / ${stats.losses}`} sublabel="settled bets" />
            <StatTile
              label="Net units (settled)"
              value={`${stats.netUnits >= 0 ? "+" : ""}${stats.netUnits.toFixed(2)}u`}
              sublabel="won bets profit at their placement price, lost bets lose the full stake"
            />
          </div>
          <div className="text-sm font-medium mb-2">Pending</div>
          <PlacedBetsTable rows={pending} showSettleActions onSettle={handleSettle} onDelete={handleDelete} />
          <div className="text-sm font-medium mb-2 mt-8">Settled</div>
          <PlacedBetsTable rows={settled} showSettleActions={false} onSettle={handleSettle} onDelete={handleDelete} />
        </>
      )}
      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        Racing bets are paper stakes for CLV (model_validated: false — racing can't be backtested). Settle
        them with the Won/Lost buttons once the race resolves; the stake stays locked out of the racing pool
        on the Markets page until settled.
      </p>
    </PageShell>
  );
}
