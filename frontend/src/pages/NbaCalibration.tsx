import { useQuery } from "@tanstack/react-query";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { StatTilesSkeleton } from "../components/ui/Skeleton";
import { fetchBetStats } from "../api/markets";

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

export function NbaCalibration() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["bet-stats", "nba"],
    queryFn: () => fetchBetStats("nba"),
  });

  const brierBetter = data && data.brier_score !== null && data.market_brier_score !== null && data.brier_score < data.market_brier_score;

  return (
    <PageShell title="NBA Calibration">
      {isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}

      {isLoading ? (
        <StatTilesSkeleton />
      ) : data ? (
        <>
          <div className="flex flex-wrap border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] mb-6 divide-x divide-[var(--color-border)]">
            <StatTile
              label="Settled bets"
              value={String(data.total_settled)}
              sublabel={`${data.wins}W / ${data.losses}L${data.pushes ? ` / ${data.pushes} push` : ""}`}
            />
            <StatTile
              label="Win rate"
              value={formatPct(data.win_rate)}
              sublabel="small samples are mostly luck -- don't over-read this early"
            />
            <StatTile
              label="Brier score (model)"
              value={data.brier_score !== null ? data.brier_score.toFixed(3) : "—"}
              sublabel={data.market_brier_score !== null ? `market: ${data.market_brier_score.toFixed(3)} (lower is better)` : "lower is better"}
            />
            <StatTile
              label="Avg. CLV"
              value={data.avg_clv_pp !== null ? `${data.avg_clv_pp >= 0 ? "+" : ""}${(data.avg_clv_pp * 100).toFixed(1)}pp` : "—"}
              sublabel={`across ${data.clv_sample_size} bet${data.clv_sample_size === 1 ? "" : "s"} with a known closing price`}
            />
          </div>

          {data.brier_score !== null && data.market_brier_score !== null && (
            <div className={"rounded-lg border px-4 py-3 mb-6 text-sm " + (brierBetter ? "border-[var(--color-good)]/30 bg-[var(--color-good)]/10" : "border-[var(--color-border)] bg-[var(--color-surface)]")}>
              {brierBetter
                ? "On the bets placed so far, the model's own probabilities have scored better (lower Brier score) than the market's implied prices -- a genuinely good early sign, though still a small sample."
                : "On the bets placed so far, the market's implied prices have scored at least as well as the model's own probabilities -- the NBA moneyline/spread/total backtest already found no free historical odds source to validate against (see Backtests), so live CLV tracking here is the real test."}
            </div>
          )}

          <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
            <div className="text-sm font-medium mb-1">Calibration</div>
            <div className="text-xs text-[var(--color-text-dim)] mb-4">
              If the model is well-calibrated, bets it rated "70-80%" should actually win about 70-80% of
              the time. Bars need a real sample to mean anything -- buckets with only 1-2 bets are just
              noise.
            </div>
            <div className="space-y-2.5">
              {data.calibration_buckets.map((b) => (
                <div key={b.range_label} className="flex items-center gap-3">
                  <div className="w-16 text-xs text-[var(--color-text-dim)] tabular-nums shrink-0">{b.range_label}</div>
                  <div className="flex-1 h-5 rounded bg-white/5 relative overflow-hidden">
                    {b.predicted_avg !== null && (
                      <div
                        className="absolute inset-y-0 left-0 bg-[var(--color-text-dim)]/30 border-r-2 border-[var(--color-text-dim)]"
                        style={{ width: `${b.predicted_avg * 100}%` }}
                        title={`Predicted avg: ${formatPct(b.predicted_avg)}`}
                      />
                    )}
                    {b.actual_win_rate !== null && (
                      <div
                        className="absolute inset-y-0 left-0 bg-[var(--color-accent)]/60"
                        style={{ width: `${b.actual_win_rate * 100}%` }}
                        title={`Actual win rate: ${formatPct(b.actual_win_rate)}`}
                      />
                    )}
                  </div>
                  <div className="w-32 text-xs text-[var(--color-text-dim)] tabular-nums shrink-0 text-right">
                    {b.n > 0 ? `${formatPct(b.actual_win_rate)} actual (n=${b.n})` : "no bets yet"}
                  </div>
                </div>
              ))}
            </div>
            <div className="flex items-center gap-4 mt-4 pt-3 border-t border-[var(--color-border)] text-xs text-[var(--color-text-dim)]">
              <span className="inline-flex items-center gap-1.5">
                <span className="w-3 h-3 rounded bg-[var(--color-text-dim)]/30 border border-[var(--color-text-dim)] inline-block" />
                Predicted (model)
              </span>
              <span className="inline-flex items-center gap-1.5">
                <span className="w-3 h-3 rounded bg-[var(--color-accent)]/60 inline-block" />
                Actual win rate
              </span>
            </div>
          </div>
        </>
      ) : null}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        Win rate needs many settled bets before it means much. Brier score and CLV are both usable with a
        much smaller sample -- for NBA specifically, live CLV tracking is the ONLY real market-quality
        check available, since no free historical NBA odds source exists to backtest against (see
        Backtests). None of this is a claim that any model here beats the market.
      </p>
    </PageShell>
  );
}
