import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { RecommendedBetsTable } from "../components/markets/RecommendedBetsTable";
import { BetReasoningModal } from "../components/markets/BetReasoningModal";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import {
  fetchSoccerMarkets,
  fetchSettings,
  fetchLockedPools,
  fetchPlacedBets,
  markBetPlaced,
  buildSoccerRecommendedBets,
  crossPlatformKey,
  type RecommendedBetRow,
} from "../api/markets";

export function SoccerRecommended() {
  const queryClient = useQueryClient();
  const marketsQuery = useQuery({ queryKey: ["soccer", "markets"], queryFn: fetchSoccerMarkets });
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const lockedQuery = useQuery({ queryKey: ["locked-pools", "soccer"], queryFn: () => fetchLockedPools("soccer") });
  const pendingBetsQuery = useQuery({
    queryKey: ["placed-bets", "soccer", "pending"],
    queryFn: () => fetchPlacedBets("pending", "soccer"),
  });

  const [reasoningRow, setReasoningRow] = useState<RecommendedBetRow | null>(null);

  const isLoading = marketsQuery.isLoading || settingsQuery.isLoading || lockedQuery.isLoading;
  const isError = marketsQuery.isError || settingsQuery.isError;

  const result = useMemo(
    () =>
      buildSoccerRecommendedBets(
        marketsQuery.data ?? [],
        settingsQuery.data?.soccer_weekly_pool_dollars ?? 0,
        lockedQuery.data?.weekly_locked_dollars ?? 0
      ),
    [marketsQuery.data, settingsQuery.data, lockedQuery.data]
  );

  const placedMarketIds = useMemo(() => new Set((pendingBetsQuery.data ?? []).map((b) => b.market_id)), [pendingBetsQuery.data]);
  const placedEntityKeys = useMemo(
    () =>
      new Set(
        // PASS THE BACKEND'S cross_key THROUGH. These are placed bets, and the
        // board's soccer rows now key off the canonical club name rather than the
        // raw label ("Fulham" / "Fulham FC"). Recomputing the raw key here would
        // have matched before that change and silently stopped matching after it,
        // so a soccer game bet you had already placed would reappear as unplaced
        // on this page -- the exact re-offer the canonicalisation exists to stop.
        (pendingBetsQuery.data ?? []).map((b) =>
          crossPlatformKey({ marketType: b.market_type, nflGameId: null, soccerMatchId: b.soccer_match_id, team: b.team, line: b.line, side: b.side, label: b.label, crossKey: b.cross_key ?? null })
        )
      ),
    [pendingBetsQuery.data]
  );
  const rows = useMemo(
    () => result.rows.filter((r) => !placedMarketIds.has(r.marketId) && !placedEntityKeys.has(crossPlatformKey(r))),
    [result.rows, placedMarketIds, placedEntityKeys]
  );
  const unitDollars = settingsQuery.data?.unit_dollars ?? 0;

  const stats = useMemo(() => {
    const totalStake = rows.reduce((sum, r) => sum + r.suggestedStakeDollars, 0);
    const avgEdge = rows.length ? rows.reduce((sum, r) => sum + Math.abs(r.edge ?? 0), 0) / rows.length : null;
    return { count: rows.length, totalStake, avgEdge };
  }, [rows]);

  const unitsLabel = (dollars: number) => (unitDollars > 0 ? ` (${(dollars / unitDollars).toFixed(1)}u)` : "");

  async function handleMarkPlaced(row: RecommendedBetRow) {
    await markBetPlaced(row);
    queryClient.invalidateQueries({ queryKey: ["placed-bets", "soccer"] });
    queryClient.invalidateQueries({ queryKey: ["locked-pools", "soccer"] });
  }

  return (
    <PageShell title="Soccer Recommended Bets">
      {isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}

      {isLoading ? (
        <>
          <StatTilesSkeleton />
          <TableSkeleton cols={8} />
        </>
      ) : (
        <>
          <div className="flex flex-wrap border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] mb-6 divide-x divide-[var(--color-border)]">
            <StatTile label="Bets shown" value={String(stats.count)} sublabel="deduped + portfolio capped, see below" />
            <StatTile
              label="Total suggested stake"
              value={`$${Math.round(stats.totalStake).toLocaleString()}${unitsLabel(stats.totalStake)}`}
              sublabel="if every position below were taken at once"
            />
            <StatTile
              label="Avg. edge"
              value={stats.avgEdge !== null ? `${(stats.avgEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="across bets shown"
            />
          </div>

          {result.rawCandidateCount > 0 && (
            <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3 mb-6 text-xs text-[var(--color-text-dim)]">
              {result.rawCandidateCount} markets cleared the staking threshold. {result.collapsedCount} were
              the same real-world bet in disguise (the same outcome priced on both Kalshi and Polymarket, or
              a different outcome from the same 3-way match) and got merged/capped.
              {result.cutByPortfolioCapCount > 0
                ? `${result.cutByPortfolioCapCount} more cleared but were cut by the portfolio cap below.`
                : "Nothing was cut by the portfolio cap."}
              {result.rows.length - rows.length > 0 &&
                ` ${result.rows.length - rows.length} already covered by a pending placed bet are hidden from this list -- see Placed Bets.`}
            </div>
          )}

          <RecommendedBetsTable rows={rows} onMarkPlaced={handleMarkPlaced} onShowReasoning={setReasoningRow} />
        </>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        A bet appears here only if quarter Kelly (capped at 5% of the pool per position — see Settings)
        computes a positive stake, which requires at least a 3pp edge. Soccer gets its own slice of your
        total cross-sport bankroll, all in one pool for now (no futures market is built yet). This app's own
        fresh backtest found the market beats this attack/defense Poisson baseline in every European league
        tested — it's a mechanical filter on unvalidated model output (model_validated: false everywhere),
        not a claim that any of these beat the market. Home advantage is one shared constant for 25 of the
        26 leagues, which was checked rather than assumed: measured on 2019-onward matches only, the shared
        constant is unbiased almost everywhere (pooled +0.2pp). Brazil&rsquo;s Série A is the single
        exception and carries its own fitted term, having stayed tilted the same way in every window
        tested and improved on held-out seasons. MLS looked similar but got worse out of sample, so it
        was rejected and keeps the shared constant. Sort by any column; use the info button for the
        reasoning behind the number.
      </p>

      {reasoningRow && (
        <BetReasoningModal
          marketId={reasoningRow.marketId}
          modelProb={reasoningRow.estProb}
          marketProb={reasoningRow.impliedProb}
          sport="soccer"
          onClose={() => setReasoningRow(null)}
        />
      )}
    </PageShell>
  );
}
