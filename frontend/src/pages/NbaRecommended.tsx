import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { RecommendedBetsTable } from "../components/markets/RecommendedBetsTable";
import { BetReasoningModal } from "../components/markets/BetReasoningModal";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import {
  fetchNbaMarkets,
  fetchNbaFutures,
  fetchSettings,
  fetchLockedPools,
  fetchPlacedBets,
  markBetPlaced,
  buildNbaRecommendedBets,
  crossPlatformKey,
  perGamePoolLabel,
  type RecommendedBetRow,
} from "../api/markets";

export function NbaRecommended() {
  const queryClient = useQueryClient();
  const marketsQuery = useQuery({ queryKey: ["nba", "markets"], queryFn: fetchNbaMarkets });
  const futuresQuery = useQuery({ queryKey: ["nba", "futures"], queryFn: fetchNbaFutures });
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const lockedQuery = useQuery({ queryKey: ["locked-pools", "nba"], queryFn: () => fetchLockedPools("nba") });
  const pendingBetsQuery = useQuery({
    queryKey: ["placed-bets", "nba", "pending"],
    queryFn: () => fetchPlacedBets("pending", "nba"),
  });

  const [reasoningRow, setReasoningRow] = useState<RecommendedBetRow | null>(null);

  const isLoading = marketsQuery.isLoading || futuresQuery.isLoading || settingsQuery.isLoading || lockedQuery.isLoading;
  const isError = marketsQuery.isError || futuresQuery.isError || settingsQuery.isError;

  const result = useMemo(
    () =>
      buildNbaRecommendedBets(
        marketsQuery.data ?? [],
        futuresQuery.data ?? [],
        settingsQuery.data?.nba_weekly_pool_dollars ?? 0,
        settingsQuery.data?.nba_futures_pool_dollars ?? 0,
        lockedQuery.data?.weekly_locked_dollars ?? 0,
        lockedQuery.data?.futures_locked_dollars ?? 0
      ),
    [marketsQuery.data, futuresQuery.data, settingsQuery.data, lockedQuery.data]
  );

  const placedMarketIds = useMemo(() => new Set((pendingBetsQuery.data ?? []).map((b) => b.market_id)), [pendingBetsQuery.data]);
  const placedEntityKeys = useMemo(
    () =>
      new Set(
        (pendingBetsQuery.data ?? []).map((b) =>
          crossPlatformKey({ marketType: b.market_type, nflGameId: null, nbaGameId: b.nba_game_id, team: b.team, line: b.line, side: b.side, label: b.label })
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
    const weeklyStake = rows.filter((r) => r.stakePool === "weekly").reduce((sum, r) => sum + r.suggestedStakeDollars, 0);
    const futuresStake = rows.filter((r) => r.stakePool === "futures").reduce((sum, r) => sum + r.suggestedStakeDollars, 0);
    const totalStake = weeklyStake + futuresStake;
    const avgEdge = rows.length ? rows.reduce((sum, r) => sum + Math.abs(r.edge ?? 0), 0) / rows.length : null;
    return { count: rows.length, totalStake, weeklyStake, futuresStake, avgEdge };
  }, [rows]);

  const unitsLabel = (dollars: number) => (unitDollars > 0 ? ` (${(dollars / unitDollars).toFixed(1)}u)` : "");

  async function handleMarkPlaced(row: RecommendedBetRow) {
    await markBetPlaced(row);
    queryClient.invalidateQueries({ queryKey: ["placed-bets", "nba"] });
    queryClient.invalidateQueries({ queryKey: ["locked-pools", "nba"] });
  }

  return (
    <PageShell title="NBA Recommended Bets">
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
            <StatTile label="Bets shown" value={String(stats.count)} sublabel="deduped + per-pool capped, see below" />
            <StatTile
              label="Total suggested stake"
              value={`$${Math.round(stats.totalStake).toLocaleString()}${unitsLabel(stats.totalStake)}`}
              sublabel="if every position below were taken at once"
            />
            <StatTile
              label={`${perGamePoolLabel("nba")} / Futures split`}
              value={`$${Math.round(stats.weeklyStake).toLocaleString()} / $${Math.round(stats.futuresStake).toLocaleString()}`}
              sublabel="NBA's own sub-allocation of the shared bankroll, see Settings"
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
              the same real-world bet in disguise (the same outcome priced on both Kalshi and Polymarket,
              or a different market type on the same game) and got merged into the single best version.
              {result.cutByPortfolioCapCount > 0
                ? `${result.cutByPortfolioCapCount} more cleared but were cut by the per-pool portfolio cap below.`
                : "Nothing was cut by the portfolio cap."}
              {result.rows.length - rows.length > 0 &&
                ` ${result.rows.length - rows.length} already covered by a pending placed bet are hidden from this list -- see Placed Bets.`}
            </div>
          )}

          <RecommendedBetsTable rows={rows} onMarkPlaced={handleMarkPlaced} onShowReasoning={setReasoningRow} />
        </>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        A bet appears here only if quarter Kelly (capped at 5% of its pool per position — see Settings)
        computes a positive stake, which requires at least a 3pp edge. NBA gets its own slice of your
        total cross-sport bankroll (separate from NFL's), split further into a DAILY pool (moneyline/
        spread/total) and a smaller FUTURES pool (championship/conference/division/win-totals), each
        capped separately. Right now every tracked game is Summer League with no baseline, so this list
        will stay empty until real regular-season inventory opens and clears an edge -- expected, not a
        bug. It's still a mechanical filter on unvalidated model output (model_validated: false
        everywhere), not a claim that any of these beat the market. Sort by any column; click a row
        with a game attached to see full detail, or use the info button for the reasoning behind the
        number.
      </p>

      {reasoningRow && (
        <BetReasoningModal
          marketId={reasoningRow.marketId}
          modelProb={reasoningRow.estProb}
          marketProb={reasoningRow.impliedProb}
          sport="nba"
          onClose={() => setReasoningRow(null)}
        />
      )}
    </PageShell>
  );
}
