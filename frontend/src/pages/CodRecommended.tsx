import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { RecommendedBetsTable } from "../components/markets/RecommendedBetsTable";
import { BetReasoningModal } from "../components/markets/BetReasoningModal";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import {
  fetchCodMarkets,
  fetchSettings,
  fetchLockedPools,
  fetchPlacedBets,
  markBetPlaced,
  buildCodRecommendedBets,
  crossPlatformKey,
  type RecommendedBetRow,
} from "../api/markets";

/** Call of Duty's own Recommended Bets page. CoD is the fifth esports title and
 * carries its own bankroll slot (settings.py::COD_ALLOCATION_PCT_KEY). */
export function CodRecommended() {
  const queryClient = useQueryClient();
  const marketsQuery = useQuery({ queryKey: ["cod", "markets"], queryFn: fetchCodMarkets });
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const lockedQuery = useQuery({ queryKey: ["locked-pools", "cod"], queryFn: () => fetchLockedPools("cod") });
  const pendingBetsQuery = useQuery({
    queryKey: ["placed-bets", "cod", "pending"],
    queryFn: () => fetchPlacedBets("pending", "cod"),
  });

  const [reasoningRow, setReasoningRow] = useState<RecommendedBetRow | null>(null);

  const isLoading = marketsQuery.isLoading || settingsQuery.isLoading || lockedQuery.isLoading;
  const isError = marketsQuery.isError || settingsQuery.isError;

  const result = useMemo(
    () =>
      buildCodRecommendedBets(
        marketsQuery.data ?? [],
        settingsQuery.data?.cod_weekly_pool_dollars ?? 0,
        // Always 0 today: neither Kalshi nor Polymarket lists CoD futures, so
        // the sub-pool is 0 by design rather than by omission.
        settingsQuery.data?.cod_futures_pool_dollars ?? 0,
        lockedQuery.data?.weekly_locked_dollars ?? 0,
        lockedQuery.data?.futures_locked_dollars ?? 0
      ),
    [marketsQuery.data, settingsQuery.data, lockedQuery.data]
  );

  const placedMarketIds = useMemo(
    () => new Set((pendingBetsQuery.data ?? []).map((b) => b.market_id)),
    [pendingBetsQuery.data]
  );
  const placedEntityKeys = useMemo(
    () =>
      new Set(
        (pendingBetsQuery.data ?? []).map((b) =>
          crossPlatformKey({
            marketType: b.market_type, sport: b.sport, nflGameId: null,
            valorantMatchId: null, cs2MatchId: null, lolMatchId: null, codMatchId: null,
            team: b.team, line: b.line, side: b.side, label: b.label,
          })
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
    queryClient.invalidateQueries({ queryKey: ["placed-bets", "cod"] });
    queryClient.invalidateQueries({ queryKey: ["locked-pools", "cod"] });
  }

  return (
    <PageShell title="Call of Duty Recommended Bets">
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
              the same real-world bet in disguise (a totals ladder rung, or the same match priced on both
              Kalshi and Polymarket) and got merged/capped.
              {result.cutByPortfolioCapCount > 0
                ? ` ${result.cutByPortfolioCapCount} more cleared but were cut by the portfolio cap below.`
                : " Nothing was cut by the portfolio cap."}
              {result.rows.length - rows.length > 0 &&
                ` ${result.rows.length - rows.length} already covered by a pending placed bet are hidden from this list -- see Placed Bets.`}
            </div>
          )}

          <RecommendedBetsTable rows={rows} onMarkPlaced={handleMarkPlaced} onShowReasoning={setReasoningRow} />
        </>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        Call of Duty gets the same 6% slice of your bankroll as every other sport (see Settings). There is no
        futures sub-pool because neither Kalshi nor Polymarket lists CoD futures — that is the real
        inventory, not a coverage gap. Markets come from Kalshi (KXCODGAME, match winner) and Polymarket
        (match winner plus total-maps ladders); a match priced on both venues is collapsed to one row.
        The model is a team-level Elo trained on 3,615 real matches (2020–2026) from breakingpoint.gg,
        scoring 64.8% walk-forward accuracy over 2,508 predictions — between this app&rsquo;s CS2 (60.8%) and
        LoL (67.1%) — blended with each pair&rsquo;s own head-to-head record where one exists, and carrying a
        fitted first-listed advantage (+10 Elo) that corrects a measured +2.9pp bias toward the
        higher-seeded side.
        It has NO player-level rating, NO roster-change boost and NO rest bonus. That is deliberate:
        breakingpoint publishes no lineups or transfers to build the first two from, and rest was TESTED and
        REJECTED for CoD (correlation +0.0125, z +0.67) despite being validated for the other three titles.
        A match already in progress is never priced — breakingpoint reports live status directly, so this is
        refused on a real flag rather than a clock.
        No backtest against real CoD market odds has been run, so nothing here claims an edge
        (model_validated: false). This is a mechanical filter on unvalidated model output.
      </p>

      {reasoningRow && (
        <BetReasoningModal
          marketId={reasoningRow.marketId}
          modelProb={reasoningRow.estProb}
          marketProb={reasoningRow.impliedProb}
          sport="cod"
          onClose={() => setReasoningRow(null)}
        />
      )}
    </PageShell>
  );
}
