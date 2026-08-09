import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { PlacedBetsTable } from "../components/markets/PlacedBetsTable";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchPlacedBets, settlePlacedBet, deletePlacedBet } from "../api/markets";

/** Call of Duty's own Placed Bets page. Unlike the older esports pages, CoD
 * bets DO auto-settle -- see the note at the bottom. */
export function CodPlaced() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: ["placed-bets", "cod"],
    queryFn: () => fetchPlacedBets(undefined, "cod"),
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
    queryClient.invalidateQueries({ queryKey: ["placed-bets", "cod"] });
    queryClient.invalidateQueries({ queryKey: ["cod", "markets"] });
  }

  async function handleDelete(id: number) {
    await deletePlacedBet(id);
    queryClient.invalidateQueries({ queryKey: ["placed-bets", "cod"] });
    queryClient.invalidateQueries({ queryKey: ["cod", "markets"] });
  }

  return (
    <PageShell title="Call of Duty Placed Bets">
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

          <div className="text-sm font-medium mb-2">Pending — capital locked until settled</div>
          <PlacedBetsTable rows={pending} showSettleActions onSettle={handleSettle} onDelete={handleDelete} />

          <div className="text-sm font-medium mb-2 mt-8">Settled</div>
          <PlacedBetsTable rows={settled} showSettleActions={false} onSettle={handleSettle} onDelete={handleDelete} />
        </>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        Call of Duty bets DO auto-settle. breakingpoint.gg publishes the winner of every completed match, the
        poller writes it to the fixture, and bet_settlement grades match-winner bets from it — the manual
        Won/Lost buttons above are a fallback, not the normal path. A bet only stays pending if its result
        has not landed yet or its team name did not resolve to either side of the fixture, in which case it is
        deliberately left alone rather than guessed at.
        A pending bet&rsquo;s stake stays locked out of CoD&rsquo;s own pool budget on the Recommended Bets
        page until it settles. CLV is computed for match-tied bets, since breakingpoint gives a real single
        start time per match.
      </p>
    </PageShell>
  );
}
