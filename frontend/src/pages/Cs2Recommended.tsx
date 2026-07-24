import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { RecommendedBetsTable } from "../components/markets/RecommendedBetsTable";
import { BetReasoningModal } from "../components/markets/BetReasoningModal";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import {
  fetchCs2Markets,
  fetchSettings,
  fetchLockedPools,
  fetchPlacedBets,
  markBetPlaced,
  buildCs2RecommendedBets,
  crossPlatformKey,
  type RecommendedBetRow,
} from "../api/markets";

/** CS2's own dedicated Recommended Bets page -- parallel to
 * ValorantRecommended.tsx, gets its own independent bankroll pool as of
 * 2026-07-20 (see settings.py::CS2_ALLOCATION_PCT_KEY). */
export function Cs2Recommended() {
  const queryClient = useQueryClient();
  const marketsQuery = useQuery({ queryKey: ["cs2", "markets"], queryFn: fetchCs2Markets });
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const lockedQuery = useQuery({ queryKey: ["locked-pools", "cs2"], queryFn: () => fetchLockedPools("cs2") });
  const pendingBetsQuery = useQuery({
    queryKey: ["placed-bets", "cs2", "pending"],
    queryFn: () => fetchPlacedBets("pending", "cs2"),
  });

  const [reasoningRow, setReasoningRow] = useState<RecommendedBetRow | null>(null);

  const isLoading = marketsQuery.isLoading || settingsQuery.isLoading || lockedQuery.isLoading;
  const isError = marketsQuery.isError || settingsQuery.isError;

  const result = useMemo(
    () =>
      buildCs2RecommendedBets(
        marketsQuery.data ?? [],
        settingsQuery.data?.cs2_weekly_pool_dollars ?? 0,
        settingsQuery.data?.cs2_futures_pool_dollars ?? 0,
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
    queryClient.invalidateQueries({ queryKey: ["placed-bets", "cs2"] });
    queryClient.invalidateQueries({ queryKey: ["locked-pools", "cs2"] });
  }

  return (
    <PageShell title="CS2 Recommended Bets">
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
              the same real-world bet in disguise (a ladder rung) and got merged/capped.
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
        CS2 gets its own 15% slice of your total bankroll (same as every other sport -- see Settings), split
        into a per-match pool and a tournament-winner futures pool. A bet appears here only if quarter Kelly
        (capped at 5% of whichever pool it belongs to) computes a positive stake, which requires at least a
        3pp edge. The model is a team-level Elo (K=32, grid-searched against a real 8,839-match historical
        liquipedia.net crawl of 94 S-Tier + A-Tier tournaments -- 60.75% walk-forward accuracy, beats the
        naive 0.5 baseline), blended with each pair's own real head-to-head series record when one exists
        (Bayesian shrinkage, real Brier improvement, 60.75% still the pure-Elo figure the K was grid-searched
        against), boosted 1.6x for a team's first 3 series after a real detected Liquipedia roster change,
        and given a rest-day rating bonus (capped at 2 days) for whichever side has had more time since its
        last real series (all real, validated Brier improvements). Where the real lineup that played can be
        resolved (38.8% of historical matches), this is blended 80/20 toward a PLAYER-level rating that rates
        individuals and aggregates to that lineup -- the single largest improvement in this app's esports
        build, and the only one that measurably narrows the gap to the market (~51% of it closed on a real
        78-event closing-price sample); matches with no resolvable lineup fall back to the team model.
        A real market-odds backtest against Kalshi's own historical trade data (85-match
        sample) found the market beats the model, same as every other sport in this app, so model_validated
        stays false. This is a mechanical filter on unvalidated model output, not a claim of edge.
      </p>

      {reasoningRow && (
        <BetReasoningModal
          marketId={reasoningRow.marketId}
          modelProb={reasoningRow.estProb}
          marketProb={reasoningRow.impliedProb}
          sport="cs2"
          onClose={() => setReasoningRow(null)}
        />
      )}
    </PageShell>
  );
}
