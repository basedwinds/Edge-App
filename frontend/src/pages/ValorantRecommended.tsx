import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { RecommendedBetsTable } from "../components/markets/RecommendedBetsTable";
import { BetReasoningModal } from "../components/markets/BetReasoningModal";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import {
  fetchValorantMarkets,
  fetchSettings,
  fetchLockedPools,
  fetchPlacedBets,
  markBetPlaced,
  buildValorantRecommendedBets,
  crossPlatformKey,
  type RecommendedBetRow,
} from "../api/markets";

/** Valorant's own dedicated Recommended Bets page -- gets its own
 * independent bankroll pool as of 2026-07-20 (see settings.py::
 * VALORANT_ALLOCATION_PCT_KEY), same as every other sport in this app.
 * Previously combined with CS2/LoL on one shared page (EsportsRecommended)
 * while all 3 shared one bankroll slot -- that page is gone now that each
 * title has its own slot, matching how NFL/NBA/MLB/MMA/Tennis/Soccer each
 * already work. */
export function ValorantRecommended() {
  const queryClient = useQueryClient();
  const marketsQuery = useQuery({ queryKey: ["valorant", "markets"], queryFn: fetchValorantMarkets });
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const lockedQuery = useQuery({ queryKey: ["locked-pools", "valorant"], queryFn: () => fetchLockedPools("valorant") });
  const pendingBetsQuery = useQuery({
    queryKey: ["placed-bets", "valorant", "pending"],
    queryFn: () => fetchPlacedBets("pending", "valorant"),
  });

  const [reasoningRow, setReasoningRow] = useState<RecommendedBetRow | null>(null);

  const isLoading = marketsQuery.isLoading || settingsQuery.isLoading || lockedQuery.isLoading;
  const isError = marketsQuery.isError || settingsQuery.isError;

  const result = useMemo(
    () =>
      buildValorantRecommendedBets(
        marketsQuery.data ?? [],
        settingsQuery.data?.valorant_weekly_pool_dollars ?? 0,
        settingsQuery.data?.valorant_futures_pool_dollars ?? 0,
        lockedQuery.data?.weekly_locked_dollars ?? 0,
        lockedQuery.data?.futures_locked_dollars ?? 0
      ),
    [marketsQuery.data, settingsQuery.data, lockedQuery.data]
  );

  const placedMarketIds = useMemo(() => new Set((pendingBetsQuery.data ?? []).map((b) => b.market_id)), [pendingBetsQuery.data]);
  const placedEntityKeys = useMemo(
    () =>
      new Set(
        (pendingBetsQuery.data ?? []).map((b) =>
          crossPlatformKey({
            marketType: b.market_type, sport: b.sport, nflGameId: null,
            valorantMatchId: null, cs2MatchId: null, lolMatchId: null,
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
    queryClient.invalidateQueries({ queryKey: ["placed-bets", "valorant"] });
    queryClient.invalidateQueries({ queryKey: ["locked-pools", "valorant"] });
  }

  return (
    <PageShell title="Valorant Recommended Bets">
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
              the same real-world bet in disguise (a ladder rung, or the same outcome priced on both Kalshi
              and Polymarket) and got merged/capped.
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
        Valorant gets its own 15% slice of your total bankroll (same as every other sport -- see Settings),
        split into a per-match pool and a tournament-winner futures pool. A bet appears here only if quarter
        Kelly (capped at 5% of whichever pool it belongs to) computes a positive stake, which requires at
        least a 3pp edge. The model is a team-level Elo (K=36, grid-searched against a real 19,644-match
        historical vlr.gg crawl under a per-map update rule -- 63.38% walk-forward accuracy, beats the
        naive 0.5 baseline), blended with each pair's own real head-to-head series record when one exists
        (Bayesian shrinkage, a real Brier improvement) and given a rest-day rating bonus (capped at 4 days)
        for whichever side has had more time since its last real series (also a real, validated Brier
        improvement). A player-level rating (real per-match vlr.gg lineups) is blended in at 40% where both
        lineups are known, but it is far weaker here than the same change was for CS2 (-0.001 vs -0.008
        Brier) and could NOT be market-validated (Valorant's closing-price sample is only 19 events) --
        Valorant's player pool is spread thin across Challengers/Game Changers, and Elo cannot calibrate
        between competitive tiers that rarely play each other, so those player ratings measure dominance
        within a tier, not absolute skill. A real
        market-odds backtest against Kalshi's own historical trade data (Map 1 only, 18-match sample) found
        the market beats the model, same as every other sport in this app, so model_validated stays false.
        This is a mechanical filter on unvalidated model output, not a claim of edge.
      </p>

      {reasoningRow && (
        <BetReasoningModal
          marketId={reasoningRow.marketId}
          modelProb={reasoningRow.estProb}
          marketProb={reasoningRow.impliedProb}
          sport="valorant"
          onClose={() => setReasoningRow(null)}
        />
      )}
    </PageShell>
  );
}
