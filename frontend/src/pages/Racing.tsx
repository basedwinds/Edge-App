import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { PageShell } from "../components/layout/PageShell";
import { TableSkeleton } from "../components/ui/Skeleton";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { fetchRacingMarkets, type RacingMarketRow } from "../api/markets";

const SERIES_LABEL: Record<string, string> = { f1: "Formula 1", irl: "IndyCar", nascar: "NASCAR" };

function marketLabel(r: RacingMarketRow): string {
  if (r.market_type === "race_winner") return "Race Winner";
  if (r.market_type === "pole") return "Pole Position";
  if (r.market_type === "top_n") return r.line === 3 ? "Podium (Top 3)" : `Top ${r.line}`;
  return r.market_type;
}

function pct(v: number | null) {
  return v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`;
}

export function Racing() {
  const { series } = useParams<{ series?: string }>();
  const query = useQuery({ queryKey: ["racing", "markets"], queryFn: fetchRacingMarkets });
  const all = query.data ?? [];
  const rows = series ? all.filter((r) => r.series === series) : all;

  const events = [...new Set(rows.map((r) => `${r.series}|${r.event}`))];
  const priced = rows.filter((r) => r.model_prob !== null).length;
  const title = series ? `Racing — ${SERIES_LABEL[series] ?? series.toUpperCase()}` : "Racing";

  return (
    <PageShell title={title}>
      {query.isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}

      <div className="rounded-lg border border-[var(--color-warning)]/30 bg-[var(--color-warning)]/10 px-4 py-3 mb-6 text-sm">
        <div className="font-medium text-[var(--color-text)]">🏁 Racing — paper-tracked for CLV, not real-money staked</div>
        <div className="text-xs text-[var(--color-text-dim)] mt-1 leading-relaxed">
          F1/IndyCar/NASCAR are priced by the grid + constructor + driver model (race finish) and a
          qualifying-Elo model (pole), and are <span className="text-[var(--color-text)]">auto-paper-logged for
          forward CLV exactly like every other sport</span> — the results show up in the <span className="text-[var(--color-text)]">CLV Tracker</span>.
          "Not staked" only means racing gets no real-money bet-size suggestion (it can't be historically
          backtested, so CLV is the sole judge), NOT that it's excluded from paper trading. Nothing has logged
          yet only because Kalshi isn't quoting racing prices this far out — that starts at the race weekend.
          Pre-qualifying prices use driver + constructor (no grid yet) and sharpen closer to the race.{" "}
          {priced} of {rows.length} markets priced across {events.length} events.
        </div>
      </div>

      {query.isLoading ? (
        <TableSkeleton cols={6} />
      ) : rows.length === 0 ? (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-6 text-sm text-[var(--color-text-dim)]">
          No open racing markets right now.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-[var(--color-border)]">
          <table className="w-full text-sm">
            <thead className="bg-[var(--color-surface)] text-[var(--color-text-dim)] text-xs">
              <tr>
                {!series && <th className="text-left px-3 py-2">Series</th>}
                <th className="text-left px-3 py-2">Market</th>
                <th className="text-left px-3 py-2">Driver</th>
                <th className="text-right px-3 py-2">Market %</th>
                <th className="text-right px-3 py-2">Model %</th>
                <th className="text-right px-3 py-2">Edge</th>
                <th className="text-right px-3 py-2">Volume</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--color-border)]">
              {rows.map((r) => (
                <tr key={`${r.event}-${r.market_type}-${r.line}-${r.driver}`} className="hover:bg-[var(--color-surface)]">
                  {!series && <td className="px-3 py-2 whitespace-nowrap text-[var(--color-text-muted)]">{SERIES_LABEL[r.series] ?? r.series}</td>}
                  <td className="px-3 py-2 whitespace-nowrap">{marketLabel(r)}</td>
                  <td className="px-3 py-2 whitespace-nowrap font-medium">{r.driver}</td>
                  <td className="px-3 py-2 text-right tabular-nums font-mono">{pct(r.implied_prob)}</td>
                  <td className="px-3 py-2 text-right tabular-nums font-mono text-[var(--color-text-dim)]" title={r.model_note ?? ""}>
                    {pct(r.model_prob)}
                    {r.model_prob !== null && (
                      <span className="ml-1 text-[9px] rounded-sm border border-[var(--color-warning)]/40 bg-[var(--color-warning)]/10 text-[var(--color-warning)] px-1">approx</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right"><EdgeBadge edge={r.edge} /></td>
                  <td className="px-3 py-2 text-right tabular-nums font-mono text-[var(--color-text-dim)]">{r.volume ? Math.round(r.volume).toLocaleString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </PageShell>
  );
}
