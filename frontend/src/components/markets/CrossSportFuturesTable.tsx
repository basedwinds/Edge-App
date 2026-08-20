import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Info } from "lucide-react";
import type { FuturesMarketRow } from "../../types/market";
import { fetchSettings, fetchOpenBets, markFuturesBetPlaced, fetchReadiness, isFuturesSportNotReady } from "../../api/markets";
import { futuresMarketName, futuresThreshold } from "../../utils/futuresLabel";
import { SourceBadge } from "./SourceBadge";
import { EdgeBadge } from "./EdgeBadge";
import { BetReasoningModal } from "./BetReasoningModal";
import type { SportKey } from "../../lib/sports";

export type CrossSportFuturesRow = FuturesMarketRow & { sport: SportKey };

// Identifies a future by the real-world proposition, NOT the market id -- so a
// future placed on Kalshi also marks its Polymarket twin (different id, same
// bet) as placed, and vice-versa. Same shape as loadCombinedFutures' dedup key.
function propKey(p: { sport: string; market_type: string; team: string | null; side: string | null; line: number | null }): string {
  return `${p.sport}|${p.market_type}|${p.team ?? ""}|${p.side ?? ""}|${p.line ?? ""}`;
}

const SPORT_LABEL: Record<string, string> = {
  nfl: "NFL", nba: "NBA", wnba: "WNBA", cfb: "CFB", mlb: "MLB", mma: "MMA",
  tennis: "Tennis", soccer: "Soccer", valorant: "Valorant", cs2: "CS2", lol: "LoL",
};

function pct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(0)}%`;
}

const EMPTY_KEYS: Set<string> = new Set(); // stable ref while placed bets load

// Cross-sport futures list for the All Bets "Futures" window: every sport's
// edge-qualified futures in one screen, with the reasoning modal. Placed futures
// land in the tracker's Futures section.
//
// A row is only PLACEABLE if the model actually sized it. Rows it declined show
// "—" and a disabled button rather than a fabricated stake -- the fallback that
// used to fill that gap was quietly offering a number for the very bets the
// safety gates had just refused.
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
      // SETTLED bets are deliberately NOT included.
      //
      // A settled bet's own market is closed -- the event finished. So a LIVE
      // row sharing its cross_key can only be a DIFFERENT event, and counting
      // settled bets here produces false "placed" badges and nothing else.
      // Measured: cross_key for a tournament future is sport|market_type|team
      // with no tournament in it, so Alexander Bublik losing ATP Kitzbuhel
      // marked Bublik as already-placed in EVERY later tennis tournament, and
      // three CS2 teams from BLAST Bounty did the same. Reported as "marked
      // placed in futures but not in the Bet Tracker", which is exactly right:
      // the tracker had nothing open, the badge was reading a finished bet.
      const open = await fetchOpenBets();
      return new Set<string>(
        open.filter((b) => b.stake_pool === "futures").map((b) => propKey(b))
      );
    },
  });
  const placedKeys = placedQuery.data ?? EMPTY_KEYS;
  const isPlaced = (row: CrossSportFuturesRow) => placedIds.has(row.id) || placedKeys.has(propKey(row));

  // A FUTURE YOU HAVE ALREADY PLACED IS NOT A RECOMMENDATION -- same reasoning
  // as RecommendedBetsTable, and asked for in the same breath ("can we clear the
  // futures from the list after they've been marked as placed too?"). Futures
  // capacity is rationed by hand off this very list, so a row you have already
  // taken is pure noise competing for attention with rows you have not.
  //
  // placedIds is THIS mount's own state, so a row you just clicked keeps its
  // "Placed" confirmation rather than vanishing under the cursor; it is gone on
  // the next load. placedKeys (persisted, from real open bets) is what actually
  // removes it, and it is empty while its query loads, so this FAILS OPEN.
  const visibleRows = useMemo(
    () => rows.filter((r) => placedIds.has(r.id) || !placedKeys.has(propKey(r))),
    [rows, placedIds, placedKeys],
  );

  async function place(row: CrossSportFuturesRow) {
    // A row the model DECLINED to size is not placeable. It used to be booked at
    // a fabricated fallback -- which meant the safety gates were showing a stake
    // for exactly the bets they had just refused. User-reported: "Freecs to win
    // LCK 2026 Season" offered at 0.25u while the market said 0.45% and the
    // model said 12.6%, a 28x disagreement that implausible_disagreement had
    // already blocked. 242 of 375 futures on the tab carried an invented stake,
    // 12 of them guard-blocked.
    if (row.suggested_stake_dollars == null) return;
    await markFuturesBetPlaced(row, row.sport, row.suggested_stake_dollars,
                               row.suggested_stake_units ?? 0);
    setPlacedIds((s) => new Set(s).add(row.id));
    queryClient.invalidateQueries({ queryKey: ["portfolio"] });
    queryClient.invalidateQueries({ queryKey: ["open-bets"] });
    queryClient.invalidateQueries({ queryKey: ["settled-bets"] });
    queryClient.invalidateQueries({ queryKey: ["placed-futures-keys"] });
    queryClient.invalidateQueries({ queryKey: ["placed-bets", row.sport] });
  }

  const stakeLabel = useMemo(() => (row: CrossSportFuturesRow) => {
    // 2dp below a unit: 1dp renders 0.25u as "0.3u", a number nobody set.
    if (row.suggested_stake_units != null) {
      const u = row.suggested_stake_units;
      return `${u < 1 ? u.toFixed(2) : u.toFixed(1)}u`;
    }
    if (row.suggested_stake_dollars != null) return `$${row.suggested_stake_dollars.toLocaleString()}`;
    return "—";   // the model declined to size this one; do not invent a number
  }, [unitDollars]);

  if (visibleRows.length === 0) {
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
          {visibleRows.map((r) => (
            <tr key={`${r.sport}-${r.id}`} className="hover:bg-[var(--color-surface)] align-top">
              <td className="px-3 py-2 text-[var(--color-text-dim)] whitespace-nowrap">{SPORT_LABEL[r.sport] ?? r.sport}</td>
              <td className="px-3 py-2 max-w-[15rem]">
                <div className="text-[var(--color-text)] leading-tight">{futuresMarketName(r)}</div>
                <div className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wide">{r.market_type}</div>
              </td>
              <td className="px-3 py-2">
                <div className="text-[var(--color-text)]">{r.team ?? r.side ?? "—"}</div>
                {futuresThreshold(r) && <div className="text-[11px] text-[var(--color-text-dim)]">{futuresThreshold(r)}</div>}
                {r.model_note && (
                  // See FuturesTable: "approx / tracking" on a row with no
                  // model number claims a rough estimate exists when none does.
                  // An unpriced row with a note is waiting on data, not approximate.
                  r.model_prob == null ? (
                    <div className="text-[10px] text-[var(--color-text-muted)]" title={r.model_note}>
                      not priced yet
                    </div>
                  ) : (
                    <div className="text-[10px] text-[var(--color-warning)]" title={r.model_note}>
                      approx / tracking
                    </div>
                  )
                )}
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
                    disabled={isPlaced(r) || r.suggested_stake_dollars == null}
                    title={r.suggested_stake_dollars == null
                      ? "The model declined to size this one — shown for tracking, not a recommendation"
                      : undefined}
                    className={
                      isPlaced(r)
                        ? "text-xs font-medium px-2 py-1 rounded-md border border-[var(--color-good)]/40 text-[var(--color-good)] whitespace-nowrap"
                        : r.suggested_stake_dollars == null
                        ? "text-xs font-medium px-2 py-1 rounded-md border border-[var(--color-border)]/40 text-[var(--color-text-muted)] whitespace-nowrap cursor-not-allowed"
                        : "text-xs font-medium px-2 py-1 rounded-md border border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)] whitespace-nowrap"
                    }
                  >
                    {isPlaced(r) ? "Placed ✓" : r.suggested_stake_dollars == null ? "Not sized" : "Mark placed"}
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
