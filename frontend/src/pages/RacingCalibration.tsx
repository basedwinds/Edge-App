import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { StatTilesSkeleton } from "../components/ui/Skeleton";
import { fetchBetStats } from "../api/markets";
import { isRacingSeries } from "../lib/sports";

const SERIES_LABEL: Record<string, string> = { f1: "Formula 1", irl: "IndyCar", nascar: "NASCAR" };

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

export function RacingCalibration() {
  const { series: seriesParam } = useParams<{ series?: string }>();
  // Narrowed, not cast: the param is whatever is in the URL.
  const series = isRacingSeries(seriesParam) ? seriesParam : "f1";
  const { data, isLoading, isError } = useQuery({
    queryKey: ["bet-stats", series],
    queryFn: () => fetchBetStats(series),
  });

  return (
    <PageShell title={`${SERIES_LABEL[series] ?? series.toUpperCase()} — Calibration`}>
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
            <StatTile label="Settled bets" value={String(data.total_settled)} sublabel={`${data.wins}W / ${data.losses}L${data.pushes ? ` / ${data.pushes} push` : ""}`} />
            <StatTile label="Win rate" value={formatPct(data.win_rate)} sublabel="small samples are mostly luck early" />
            <StatTile label="Brier (model)" value={data.brier_score !== null ? data.brier_score.toFixed(3) : "—"} sublabel={data.market_brier_score !== null ? `market: ${data.market_brier_score.toFixed(3)}` : "lower is better"} />
            <StatTile label="Avg. CLV" value={data.avg_clv_pp !== null ? `${data.avg_clv_pp >= 0 ? "+" : ""}${(data.avg_clv_pp * 100).toFixed(1)}pp` : "—"} sublabel={`${data.clv_sample_size} bet${data.clv_sample_size === 1 ? "" : "s"} with a close`} />
          </div>
          <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
            <div className="text-sm font-medium mb-1">Calibration</div>
            <div className="text-xs text-[var(--color-text-dim)] mb-4">
              If the model is well-calibrated, drivers it rates "10%" to win/pole should win/pole about 10% of
              the time. Needs a real sample — racing's markets are thin, so this fills in slowly.
            </div>
            <div className="space-y-2.5">
              {data.calibration_buckets.map((b) => (
                <div key={b.range_label} className="flex items-center gap-3">
                  <div className="w-16 text-xs text-[var(--color-text-dim)] tabular-nums shrink-0">{b.range_label}</div>
                  <div className="flex-1 h-5 rounded bg-white/5 relative overflow-hidden">
                    {b.predicted_avg !== null && <div className="absolute inset-y-0 left-0 bg-[var(--color-text-dim)]/30 border-r-2 border-[var(--color-text-dim)]" style={{ width: `${b.predicted_avg * 100}%` }} />}
                    {b.actual_win_rate !== null && <div className="absolute inset-y-0 left-0 bg-[var(--color-accent)]/60" style={{ width: `${b.actual_win_rate * 100}%` }} />}
                  </div>
                  <div className="w-32 text-xs text-[var(--color-text-dim)] tabular-nums shrink-0 text-right">{b.n > 0 ? `${formatPct(b.actual_win_rate)} (n=${b.n})` : "no bets yet"}</div>
                </div>
              ))}
            </div>
          </div>
        </>
      ) : null}
      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        Racing is unbacktested (model_validated: false), so forward CLV here is the only real test of whether
        the model beats the market — Brier and CLV are usable on a smaller sample than win rate.
      </p>
    </PageShell>
  );
}
