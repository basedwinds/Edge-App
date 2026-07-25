import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Info } from "lucide-react";
import type { FuturesMarketRow } from "../../types/market";
import { fetchSettings, fetchOpenBets, fetchSettledBets, markFuturesBetPlaced, fetchReadiness, isFuturesSportNotReady } from "../../api/markets";
import { futuresMarketName, futuresThreshold } from "../../utils/futuresLabel";
import { SourceBadge } from "./SourceBadge";
import { EdgeBadge } from "./EdgeBadge";
import { BetReasoningModal } from "./BetReasoningModal";

type SportKey = "nfl" | "nba" | "wnba" | "mlb" | "mma" | "tennis" | "soccer" | "valorant" | "cs2" | "lol";
export type CrossSportFuturesRow = FuturesMarketRow & { sport: SportKey };

// Identifies a future by the real-world proposition, NOT the market id -- so a
// future placed on Kalshi also marks its Polymarket twin (different id, same
// bet) as placed, and vice-versa. Same shape as loadCombinedFutures' dedup key.
function propKey(p: { sport: string; market_type: string; team: string | null; side: string | null; line: number | null }): string {
  return `${p.sport}|${p.market_type}|${p.team ?? ""}|${p.side ?? ""}|${p.line ?? ""}`;
}

const SPORT_LABEL: Record<string, string> = {
  nfl: "NFL", nba: "NBA", wnba: "WNBA", mlb: "MLB", mma: "MMA",
  tennis: "Tennis", soccer: "Soccer", valorant: "Valorant", cs2: "CS2", lol: "LoL",
};

function pct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(0)}%`;
}

const EMPTY_KEYS: Set<string> = new Set(); // stable ref while placed bets load

// Cross-sport, place-able futures list for the All Bets "Futures" window: every
// sport's edge-qualified futures in one screen, with Mark placed (falls back to
// 1 unit when the model didn't size one -- many futures are approx/tracking-only)
// and the reasoning modal. Placed futures land in the tracker's Futures section.
export function CrossSportFuturesTable({ rows: allRows }: { rows: CrossSportFuturesRow[] }) {
  const queryClient = useQueryClient();
  // Hide not-ready season-sport futures (e.g. NFL before the season) -- same
  // readiness rule as the alerts + the per-sport Futures pages.
  const readiness = useQuery({ queryKey: ["readiness"], queryFn: fetchReadiness }).data;
  const rows = useMemo(() => allRows.filter((r) => !isFuturesSportNotReady(r.sport, readiness)), [allRows, readiness]);
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const [reasoning, setReasoning] = useState<CrossSportFuturesRow | null>(null);
  const [placedIds, setPlacedIds] = useState<Set<number>>(new Set());
  const unitDollars = settingsQuery.data?.unit_dollars ?? 0;

  // Proposition keys of every futures bet already placed (open or settled), so a
  // future you placed last night still reads "Placed ✓" here and can't be
  // double-placed. Refetched after each placement.
  const placedQuery = useQuery({
    queryKey: ["placed-futures-keys"],
    queryFn: async () => {
      const [open, settled] = await Promise.all([fetchOpenBets(), fetchSettledBets()]);
      return new Set<string>(
        [...open, ...settled].filter((b) => b.stake_pool === "futures").map((b) => propKey(b))
      );
    },
  });
  const placedKeys = placedQuery.data ?? EMPTY_KEYS;
  const isPlaced = (row: CrossSportFuturesRow) => placedIds.has(row.id) || placedKeys.has(propKey(row));

  async function place(row: CrossSportFuturesRow) {
    const stakeDollars = row.suggested_stake_dollars ?? (unitDollars > 0 ? unitDollars : 10);
    const stakeUnits = row.suggested_stake_units ?? (unitDollars > 0 ? 1 : null);
    await markFuturesBetPlaced(row, row.sport, stakeDollars, stakeUnits);
    setPlacedIds((s) => new Set(s).add(row.id));
    queryClient.invalidateQueries({ queryKey: ["portfolio"] });
    queryClient.invalidateQueries({ queryKey: ["open-bets"] });
    queryClient.invalidateQueries({ queryKey: ["settled-bets"] });
    queryClient.invalidateQueries({ queryKey: ["placed-futures-keys"] });
    queryClient.invalidateQueries({ queryKey: ["placed-bets", row.sport] });
  }

  const stakeLabel = useMemo(() => (row: CrossSportFuturesRow) => {
    if (row.suggested_stake_units != null) return `${row.suggested_stake_units.toFixed(1)}u`;
    if (row.suggested_stake_dollars != null) return `$${row.suggested_stake_dollars.toLocaleString()}`;
    return unitDollars > 0 ? "1u" : "$10"; // fallback used on place
  }, [unitDollars]);

  if (rows.length === 0) {
    return (
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-6 text-sm text-[var(--color-text-dim)]">
        No edge-qualified futures right now (≥3pp model-vs-market disagreement). Futures are season-long/tournament markets — they show here so you can place them for calibration tracking.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-[var(--color-border)]">
      <table className="w-full text-sm">
        <thead className="bg-[var(--color-surface)] text-[var(--color-text-dim)] text-xs">
          <tr>
            <th className="text-left px-3 py-2">Sport</th>
            <th className="text-left px-3 py-2">Market</th>
            <th className="text-left px-3 py-2">Pick</th>
            <th className="text-right px-3 py-2">Market %</th>
            <th className="text-right px-3 py-2">Model %</th>
            <th className="text-right px-3 py-2">Edge</th>
            <th className="text-right px-3 py-2">Stake</th>
            <th className="px-3 py-2"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--color-border)]">
          {rows.map((r) => (
            <tr key={`${r.sport}-${r.id}`} className="hover:bg-[var(--color-surface)] align-top">
              <td className="px-3 py-2 text-[var(--color-text-dim)] whitespace-nowrap">{SPORT_LABEL[r.sport] ?? r.sport}</td>
              <td className="px-3 py-2 max-w-[15rem]">
                <div className="text-[var(--color-text)] leading-tight">{futuresMarketName(r)}</div>
                <div className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wide">{r.market_type}</div>
              </td>
              <td className="px-3 py-2">
                <div className="text-[var(--color-text)]">{r.team ?? r.side ?? "—"}</div>
                {futuresThreshold(r) && <div className="text-[11px] text-[var(--color-text-dim)]">{futuresThreshold(r)}</div>}
                {r.model_note && <div className="text-[10px] text-[var(--color-warning)]">approx / tracking</div>}
              </td>
              <td className="px-3 py-2 text-right tabular-nums font-mono">{pct(r.implied_prob)}</td>
              <td className="px-3 py-2 text-right tabular-nums font-mono text-[var(--color-text-dim)]">{pct(r.model_prob)}</td>
              <td className="px-3 py-2 text-right"><EdgeBadge edge={r.edge} /></td>
              <td className="px-3 py-2 text-right tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">{stakeLabel(r)}</td>
              <td className="px-3 py-2">
                <div className="flex items-center justify-end gap-1.5">
                  <SourceBadge source={r.source} />
                  <button
                    onClick={() => setReasoning(r)}
                    className="p-1 rounded-md border border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)]"
                    title="Why this number? (model explanation)"
                  >
                    <Info size={13} />
                  </button>
                  <button
                    onClick={() => place(r)}
                    disabled={isPlaced(r)}
                    className={
                      isPlaced(r)
                        ? "text-xs font-medium px-2 py-1 rounded-md border border-[var(--color-good)]/40 text-[var(--color-good)] whitespace-nowrap"
                        : "text-xs font-medium px-2 py-1 rounded-md border border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)] whitespace-nowrap"
                    }
                  >
                    {isPlaced(r) ? "Placed ✓" : "Mark placed"}
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {reasoning && (
        <BetReasoningModal
          marketId={reasoning.id}
          modelProb={reasoning.model_prob}
          marketProb={reasoning.implied_prob}
          sport={reasoning.sport}
          onClose={() => setReasoning(null)}
        />
      )}
    </div>
  );
}
