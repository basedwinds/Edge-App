import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { Info } from "lucide-react";
import { PageShell } from "../components/layout/PageShell";
import { TableSkeleton } from "../components/ui/Skeleton";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { BetReasoningModal } from "../components/markets/BetReasoningModal";
import { fetchRacingMarkets, fetchSettings, markRacingBetPlaced, type RacingMarketRow } from "../api/markets";

const SERIES_LABEL: Record<string, string> = { f1: "Formula 1", irl: "IndyCar", nascar: "NASCAR" };
const CHAMP_TYPES = new Set(["drivers_champion", "constructors_champion"]);

function champLabel(r: RacingMarketRow): string {
  if (r.market_type === "drivers_champion") return "Drivers' Champion";
  if (r.market_type === "constructors_champion") return "Constructors' Champion";
  return r.market_type;
}

function pct(v: number | null) {
  return v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`;
}

export function RacingFutures() {
  const { series } = useParams<{ series?: string }>();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["racing", "markets"], queryFn: fetchRacingMarkets });
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const unitDollars = settingsQuery.data?.unit_dollars ?? 0;
  const [placedIds, setPlacedIds] = useState<Set<number>>(new Set());
  const [reasoning, setReasoning] = useState<RacingMarketRow | null>(null);

  async function place(r: RacingMarketRow) {
    const stakeDollars = r.suggested_stake_dollars ?? (unitDollars > 0 ? unitDollars : 10);
    const stakeUnits = r.suggested_stake_units ?? (unitDollars > 0 ? 1 : null);
    await markRacingBetPlaced(r, stakeDollars, stakeUnits);
    setPlacedIds((s) => new Set(s).add(r.id));
    queryClient.invalidateQueries({ queryKey: ["portfolio"] });
    queryClient.invalidateQueries({ queryKey: ["open-bets"] });
  }

  const all = query.data ?? [];
  const rows = all
    .filter((r) => (series ? r.series === series : true) && CHAMP_TYPES.has(r.market_type))
    // Drivers' first, then by the model's title probability (favourites on top).
    .slice()
    .sort((a, b) => a.market_type.localeCompare(b.market_type) || (b.model_prob ?? 0) - (a.model_prob ?? 0));

  const title = series ? `Racing Futures — ${SERIES_LABEL[series] ?? series.toUpperCase()}` : "Racing Futures";

  return (
    <PageShell title={title}>
      <div className="rounded-lg border border-[var(--color-accent)]/30 bg-[var(--color-accent)]/10 px-4 py-3 mb-6 text-sm">
        <div className="font-medium text-[var(--color-text)]">🏆 Season championship futures</div>
        <div className="text-xs text-[var(--color-text-dim)] mt-1 leading-relaxed">
          Drivers'/Constructors' Champion, priced by a <span className="text-[var(--color-text)]">standings-aware
          season Monte Carlo</span> — it simulates the remaining races from the current championship points and
          each driver's strength, so pace alone can't price a title (a fast car far back is still a long shot).
          Compared against <span className="text-[var(--color-text)]">Polymarket</span>. Still
          {" "}<span className="text-[var(--color-text)]">model_validated: false</span> — forward CLV is the judge,
          so treat stakes as paper.
        </div>
      </div>

      {query.isLoading ? (
        <TableSkeleton cols={6} />
      ) : rows.length === 0 ? (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-6 text-sm text-[var(--color-text-dim)]">
          No season futures priced right now. F1 lists Drivers'/Constructors' Champion markets; NASCAR's title is a
          playoff-elimination format that isn't yet traded on either book, so it stays unpriced until it goes live.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-[var(--color-border)]">
          <table className="w-full text-sm">
            <thead className="bg-[var(--color-surface)] text-[var(--color-text-dim)] text-xs">
              <tr>
                <th className="text-left px-3 py-2">Market</th>
                <th className="text-left px-3 py-2">Driver / Constructor</th>
                <th className="text-right px-3 py-2">Market %</th>
                <th className="text-right px-3 py-2">Model %</th>
                <th className="text-right px-3 py-2">Edge</th>
                <th className="text-right px-3 py-2">Stake</th>
                <th className="text-right px-3 py-2">Volume</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--color-border)]">
              {rows.map((r) => (
                <tr key={`${r.event}-${r.market_type}-${r.driver}`} className="hover:bg-[var(--color-surface)]">
                  <td className="px-3 py-2 whitespace-nowrap">{champLabel(r)}</td>
                  <td className="px-3 py-2 whitespace-nowrap font-medium">{r.driver}</td>
                  <td className="px-3 py-2 text-right tabular-nums font-mono">{pct(r.implied_prob)}</td>
                  <td className="px-3 py-2 text-right tabular-nums font-mono text-[var(--color-text-dim)]" title={r.model_note ?? ""}>
                    {pct(r.model_prob)}
                    {r.model_prob !== null && (
                      <span className="ml-1 text-[9px] rounded-sm border border-[var(--color-warning)]/40 bg-[var(--color-warning)]/10 text-[var(--color-warning)] px-1">approx</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right"><EdgeBadge edge={r.edge} /></td>
                  <td className="px-3 py-2 text-right tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">
                    {r.suggested_stake_units != null ? `${r.suggested_stake_units.toFixed(1)}u` : r.suggested_stake_dollars != null ? `$${r.suggested_stake_dollars}` : "—"}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums font-mono text-[var(--color-text-dim)]">{r.volume ? Math.round(r.volume).toLocaleString() : "—"}</td>
                  <td className="px-3 py-2">
                    <div className="flex items-center justify-end gap-1.5">
                      {r.model_prob != null && (
                        <button
                          onClick={() => setReasoning(r)}
                          className="p-1 rounded-md border border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)]"
                          title="Why this number? (model explanation)"
                        >
                          <Info size={13} />
                        </button>
                      )}
                      {r.suggested_stake_dollars != null && (
                        <button
                          onClick={() => place(r)}
                          disabled={placedIds.has(r.id)}
                          className={placedIds.has(r.id)
                            ? "text-xs font-medium px-2 py-1 rounded-md border border-[var(--color-good)]/40 text-[var(--color-good)] whitespace-nowrap"
                            : "text-xs font-medium px-2 py-1 rounded-md border border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)] whitespace-nowrap"}
                        >
                          {placedIds.has(r.id) ? "Placed ✓" : "Mark placed"}
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {reasoning && (
        <BetReasoningModal
          marketId={reasoning.id}
          modelProb={reasoning.model_prob}
          marketProb={reasoning.implied_prob}
          sport={reasoning.series}
          onClose={() => setReasoning(null)}
        />
      )}
    </PageShell>
  );
}
