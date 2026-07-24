import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { PageShell } from "../components/layout/PageShell";
import { TableSkeleton } from "../components/ui/Skeleton";
import { fetchDivergences, type DivergenceRow } from "../api/markets";

function pct(p: number) {
  return `${(p * 100).toFixed(0)}%`;
}

const SPORT_LABEL: Record<string, string> = {
  nfl: "NFL", nba: "NBA", wnba: "WNBA", mlb: "MLB", mma: "MMA",
  tennis: "Tennis", soccer: "Soccer", valorant: "Valorant", cs2: "CS2", lol: "LoL",
};
const PLATFORM = { kalshi: "Kalshi", polymarket: "Polymarket" } as const;

// A gap is only real money if you can actually FILL both sides. These are
// deliberately loose "is there a pulse" thresholds, not precise fill models --
// volume units differ across the two platforms, so treat them as a flag to go
// look, not a guarantee. Anything below THIN on either side gets a warning.
const THIN = 100;
const STRONG_GAP = 0.05;
const STRONG_VOL = 500;

type Tier = "strong" | "ok" | "thin";
function tierOf(r: DivergenceRow): Tier {
  const kv = r.kalshi_volume ?? 0;
  const pv = r.polymarket_volume ?? 0;
  const minv = Math.min(kv, pv);
  if (r.kalshi_volume == null || r.polymarket_volume == null || minv < THIN) return "thin";
  if (r.gap >= STRONG_GAP && minv >= STRONG_VOL) return "strong";
  return "ok";
}
const TIER_BADGE: Record<Tier, { label: string; cls: string }> = {
  strong: { label: "Strong", cls: "bg-[var(--color-good)]/15 text-[var(--color-good)] border-[var(--color-good)]/30" },
  ok: { label: "Playable", cls: "bg-[var(--color-accent)]/15 text-[var(--color-accent)] border-[var(--color-accent)]/30" },
  thin: { label: "Thin — verify fill", cls: "bg-[var(--color-warning)]/15 text-[var(--color-warning)] border-[var(--color-warning)]/30" },
};

function vol(v: number | null): string {
  if (v == null) return "no vol";
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`;
  return `${Math.round(v)}`;
}

const GAP_FILTERS = [
  { key: 0.03, label: "≥ 3pp" },
  { key: 0.05, label: "≥ 5pp" },
  { key: 0.07, label: "≥ 7pp" },
];

export function Divergences() {
  const [minGap, setMinGap] = useState(0.05);
  const query = useQuery({ queryKey: ["divergences", minGap], queryFn: () => fetchDivergences(minGap) });
  const rows: DivergenceRow[] = (query.data ?? []).slice().sort((a, b) => b.gap - a.gap);

  return (
    <PageShell title="Cross-Platform Divergences">
      {/* Plain-language "what am I looking at / what do I do" primer. */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3 mb-5 text-[13px] text-[var(--color-text-dim)] leading-relaxed max-w-3xl">
        <div className="text-[var(--color-text)] font-medium mb-1">The play, in one line</div>
        The <em>same</em> real-world outcome is priced differently on Kalshi vs Polymarket. Buy the <strong>cheaper</strong> side.
        The clean version is a two-sided lock: buy <strong>YES on the cheap platform</strong> and <strong>NO on the pricey one</strong> —
        your total cost is <span className="font-mono">1 − gap</span>, so you pocket roughly the gap <em>no matter who wins</em>, if you
        can fill both sides. That "if" is the whole game: it only works when both sides actually have liquidity, and the gap has to clear
        each platform's fees. So work the biggest gaps on the thickest markets first, and skip anything flagged <span className="text-[var(--color-warning)]">Thin</span>.
      </div>

      <div className="flex items-center gap-1.5 mb-4">
        <span className="text-xs text-[var(--color-text-muted)] mr-1">Min gap:</span>
        {GAP_FILTERS.map((g) => (
          <button
            key={g.key}
            onClick={() => setMinGap(g.key)}
            className={
              minGap === g.key
                ? "text-xs font-medium px-2.5 py-1 rounded-md bg-[var(--color-accent)] text-[#1c1408]"
                : "text-xs font-medium px-2.5 py-1 rounded-md border border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)]"
            }
          >
            {g.label}
          </button>
        ))}
        <span className="ml-1 text-[11px] text-[var(--color-text-muted)]">3pp barely clears fees — 5pp+ is where it gets interesting</span>
      </div>

      {query.isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}

      {query.isLoading ? (
        <TableSkeleton cols={7} />
      ) : rows.length === 0 ? (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-6 text-sm text-[var(--color-text-dim)]">
          No pre-game divergences ≥ {(minGap * 100).toFixed(0)}pp right now. This scans upcoming (not
          started/finished) games where the same proposition has traded on both platforms. Loosen the min gap
          to see more.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-[var(--color-border)]">
          <table className="w-full text-sm">
            <thead className="bg-[var(--color-surface)] text-[var(--color-text-dim)] text-xs">
              <tr>
                <th className="text-left px-3 py-2">Game / Pick</th>
                <th className="text-right px-3 py-2">Kalshi</th>
                <th className="text-right px-3 py-2">Polymarket</th>
                <th className="text-right px-3 py-2">Gap</th>
                <th className="text-left px-3 py-2">The play</th>
                <th className="text-left px-3 py-2">Quality</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--color-border)]">
              {rows.map((r) => {
                const tier = tierOf(r);
                const badge = TIER_BADGE[tier];
                const pick = r.team ?? r.side ?? r.market_type;
                const buyPrice = r.buy_side === "kalshi" ? r.kalshi_prob : r.polymarket_prob;
                const cheap = PLATFORM[r.buy_side];
                const pricey = PLATFORM[r.buy_side === "kalshi" ? "polymarket" : "kalshi"];
                return (
                  <tr key={`${r.kalshi_market_id}-${r.polymarket_market_id}`} className="hover:bg-[var(--color-surface)] align-top">
                    <td className="px-3 py-2">
                      <div className="text-[var(--color-text)]">{pick}</div>
                      <div className="text-[11px] text-[var(--color-text-muted)]">
                        {(SPORT_LABEL[r.sport] ?? r.sport)} · {r.market_type}{r.line != null ? ` ${r.line}` : ""}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      <div>{pct(r.kalshi_prob)}</div>
                      <div className="text-[11px] text-[var(--color-text-muted)]">{vol(r.kalshi_volume)}</div>
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      <div>{pct(r.polymarket_prob)}</div>
                      <div className="text-[11px] text-[var(--color-text-muted)]">{vol(r.polymarket_volume)}</div>
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums font-medium">{(r.gap * 100).toFixed(1)}pp</td>
                    <td className="px-3 py-2">
                      <div className="text-[var(--color-text)]">
                        Buy <span className="text-[var(--color-accent)] font-medium">{pick}</span> on {cheap} @ {pct(buyPrice)}
                      </div>
                      <div className="text-[11px] text-[var(--color-text-muted)]">
                        lock it: NO on {pricey} → ~{(r.gap * 100).toFixed(1)}pp either way
                      </div>
                    </td>
                    <td className="px-3 py-2">
                      <span className={`inline-block text-[11px] px-1.5 py-0.5 rounded-full border ${badge.cls}`}>{badge.label}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-3xl">
        Model-INDEPENDENT: convergence pays regardless of whether any Elo is right, which is why this is worth
        watching even though none of the models beats the market on average. Only game-tied markets show (both
        platforms carry this app's canonical game id, so the match is reliable), pre-game only, both sides must
        have traded. <strong>Quality</strong> combines gap size with the thinner side's volume: Strong = ≥5pp
        on a liquid pair, Playable = a real gap with some volume on both, Thin = the gap's there but one side is
        illiquid or untraded (a large gap on a thin book usually can't be filled at that price — verify on the
        exchange before committing). Volume units differ between platforms, so treat them as a go-look signal,
        not a precise fill estimate.
      </p>
    </PageShell>
  );
}
