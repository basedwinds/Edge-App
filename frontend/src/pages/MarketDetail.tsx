import { useMemo, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, TriangleAlert, Newspaper, RefreshCw } from "lucide-react";
import { PageShell } from "../components/layout/PageShell";
import { SourceBadge } from "../components/markets/SourceBadge";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { ProbabilityBar } from "../components/markets/ProbabilityBar";
import { SpreadTotalLadder } from "../components/markets/SpreadTotalLadder";
import { fetchMarkets, groupByGameAndSource, refreshNews } from "../api/markets";

const LADDER_MARKET_TYPES = new Set(["spread", "total", "team_total", "spread_1h", "spread_2h", "total_1h", "total_2h"]);

export function MarketDetail() {
  const { key } = useParams<{ key: string }>();
  const decodedKey = key ? decodeURIComponent(key) : "";
  const queryClient = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["markets"],
    queryFn: fetchMarkets,
  });

  const row = useMemo(() => {
    if (!data) return null;
    return groupByGameAndSource(data).find((r) => r.key === decodedKey) ?? null;
  }, [data, decodedKey]);

  const ladderRows = useMemo(() => {
    if (!data || !row) return [];
    return data.filter(
      (r) => r.nfl_game_id === row.nfl_game_id && r.source === row.source && LADDER_MARKET_TYPES.has(r.market_type)
    );
  }, [data, row]);

  const awayTeam = row ? row.game_label.split(" @ ")[0] : "";

  async function handleRefreshNews() {
    if (!row) return;
    setRefreshing(true);
    try {
      await refreshNews(row.nfl_game_id);
      await queryClient.invalidateQueries({ queryKey: ["markets"] });
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <PageShell title="Market detail">
      <Link
        to="/"
        className="inline-flex items-center gap-1.5 text-sm text-[var(--color-text-dim)] hover:text-[var(--color-text)] mb-5 transition-colors"
      >
        <ArrowLeft size={15} />
        Back to dashboard
      </Link>

      {isLoading && <div className="text-[var(--color-text-dim)]">Loading…</div>}

      {!isLoading && !row && (
        <div className="text-[var(--color-text-dim)]">Market not found — it may no longer be tracked.</div>
      )}

      {row && (
        <div className="max-w-2xl">
          <div className="flex items-center gap-3 mb-1">
            <h2 className="text-xl font-semibold">{row.game_label}</h2>
            <SourceBadge source={row.source} />
          </div>
          <div className="text-sm text-[var(--color-text-dim)] mb-6">
            {new Date(row.gameday + "T00:00:00").toLocaleDateString(undefined, {
              weekday: "long",
              month: "long",
              day: "numeric",
              year: "numeric",
            })}{" "}
            · Moneyline · Home team: {row.homeTeam}
          </div>

          <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-6 space-y-6">
            <ProbabilityBar
              label="Market-implied probability (home win)"
              value={row.homeImpliedProb}
              color="var(--color-accent)"
              sublabel="Derived from live bid/ask on the tracked source"
            />
            <ProbabilityBar
              label="Model estimate (home win)"
              value={row.homeModelProb}
              color="#9085e9"
              sublabel={row.noBaselineReason ?? "Elo baseline"}
            />
            {row.hasNews && (
              <ProbabilityBar
                label="Final estimate (Elo + news adjustment)"
                value={row.homeFinalProb}
                color="#33c17a"
                sublabel={`News confidence: ${row.newsConfidence ?? "—"}`}
              />
            )}

            <div className="flex items-center justify-between pt-2 border-t border-[var(--color-border)]">
              <span className="text-sm text-[var(--color-text-dim)]">Disagreement (model vs market)</span>
              <EdgeBadge edge={row.edge} />
            </div>

            {row.predictedHomeScore !== null && row.predictedAwayScore !== null && (
              <div className="flex items-center justify-between pt-2 border-t border-[var(--color-border)]">
                <span className="text-sm text-[var(--color-text-dim)]" title="Combines the spread model's expected margin with the totals model's expected combined score">
                  Predicted score
                </span>
                <span className="text-lg font-semibold tabular-nums">
                  {row.homeTeam} {row.predictedHomeScore.toFixed(0)} – {awayTeam} {row.predictedAwayScore.toFixed(0)}
                </span>
              </div>
            )}
          </div>

          <SpreadTotalLadder rows={ladderRows} homeTeam={row.homeTeam} awayTeam={awayTeam} />

          <div className="mt-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3 flex items-center justify-between">
            <div className="flex items-center gap-2 text-sm text-[var(--color-text-dim)]">
              <Newspaper size={16} />
              {row.hasNews
                ? "News/research adjustment on file for this game."
                : "No news/research adjustment yet for this game."}
              {row.newsRequiresReview && (
                <span className="text-[var(--color-warning)] font-medium">— flagged for manual review</span>
              )}
            </div>
            <button
              onClick={handleRefreshNews}
              disabled={refreshing}
              className="inline-flex items-center gap-1.5 rounded-md border border-[var(--color-border)] px-3 py-1.5 text-sm hover:bg-[var(--color-surface-hover)] disabled:opacity-50 transition-colors"
            >
              <RefreshCw size={14} className={refreshing ? "animate-spin" : ""} />
              {refreshing ? "Researching…" : "Refresh news"}
            </button>
          </div>

          <div className="mt-4 rounded-lg border border-[var(--color-warning)]/30 bg-[var(--color-warning)]/10 px-4 py-3 flex gap-3">
            <TriangleAlert size={18} className="text-[var(--color-warning)] shrink-0 mt-0.5" />
            <p className="text-sm text-[var(--color-text-dim)]">
              The Elo baseline was walk-forward backtested against 10 seasons of historical closing
              lines and did <span className="text-[var(--color-text)]">not</span> beat the market's own
              pricing on NFL moneylines. The news adjustment is bounded and residual-only (never a
              fresh probability guess) but is not independently backtested either. Treat all of the
              above as data points to investigate, not a validated betting signal.
            </p>
          </div>
        </div>
      )}
    </PageShell>
  );
}
