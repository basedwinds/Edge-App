import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { SourceBadge } from "../components/markets/SourceBadge";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { describeTennisSpread, TENNIS_MARKET_TYPE_LABELS } from "../utils/tennisLabel";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchTennisMarkets } from "../api/markets";
import type { TennisMarketRow } from "../types/market";

const TIER_LABELS: Record<string, string> = {
  tour: "Tour",
  challenger: "Challenger",
  itf: "ITF",
};

// set_spread was missing here too, so this page fell to the `default` branch
// and showed only the player name -- no handicap, no line. Names now come
// from the shared table so the two pages cannot drift.
const MARKET_TYPE_LABELS: Record<string, string> = {
  moneyline: "Moneyline",
  ...TENNIS_MARKET_TYPE_LABELS,
};

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function bestEdge(r: TennisMarketRow): number | null {
  return r.model_prob !== null && r.implied_prob !== null ? r.model_prob - r.implied_prob : null;
}

function formatMatchDate(matchDate: string | null): string {
  if (!matchDate) return "—";
  const d = new Date(matchDate + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// Spells out exactly what each row's pick means -- same "don't make the
// reader decode raw fields" convention as RecommendedBetsTable.tsx's
// describePick.
function formatPick(r: TennisMarketRow): string {
  switch (r.market_type) {
    case "set_winner":
      return r.line !== null ? `${r.team ?? "—"} wins Set ${Math.round(r.line)}` : (r.team ?? "—");
    case "exact_score":
      return r.side ? `${r.team ?? "—"} wins ${r.side}` : (r.team ?? "—");
    case "game_total":
      return r.line !== null ? `Over ${r.line} games` : "—";
    // REAL BUG: this used Math.ceil(line) on the RAW signed number, so a
    // negative line rendered as "wins by -1+ games" -- and it had no
    // set_spread case at all. Both go through the shared describer now,
    // which also knows the two markets use opposite sign conventions.
    case "game_spread":
    case "set_spread":
      return describeTennisSpread(r.market_type, r.team, r.line) ?? (r.team ?? "—");
    case "set_total": {
      const setLabel = r.side ? r.side.replace("set_", "Set ") : "?";
      return r.line !== null ? `${setLabel}: Over ${r.line} games` : "—";
    }
    case "total_sets":
      return r.line !== null ? `Over ${r.line} sets` : "—";
    default:
      return r.team ?? "—";
  }
}

function TennisMarketsTable({ rows }: { rows: TennisMarketRow[] }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-x-auto">
      <table className="w-full text-sm border-collapse min-w-[960px]">
        <thead>
          <tr className="border-b border-[var(--color-border)]">
            {["Date", "Match", "Tier", "Market", "Pick", "Source", "Market %", "Est. %", "Edge", "Volume", "Note"].map((h) => (
              <th key={h} className="text-left px-4 py-3 text-xs uppercase tracking-wide text-[var(--color-text-dim)] font-medium whitespace-nowrap">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-surface-hover)] transition-colors">
              <td className="px-4 py-3 whitespace-nowrap font-mono text-[var(--color-text-dim)]">{formatMatchDate(r.match_date)}</td>
              <td className="px-4 py-3 whitespace-nowrap">{r.match_label ?? "—"}</td>
              <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-dim)]">
                {r.tour ? r.tour.toUpperCase() : ""} {TIER_LABELS[r.tier ?? ""] ?? r.tier ?? "—"}
              </td>
              <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-dim)]">{MARKET_TYPE_LABELS[r.market_type] ?? r.market_type}</td>
              <td className="px-4 py-3 font-medium whitespace-nowrap">{formatPick(r)}</td>
              <td className="px-4 py-3"><SourceBadge source={r.source} /></td>
              <td className="px-4 py-3 tabular-nums font-mono whitespace-nowrap">{formatPct(r.implied_prob)}</td>
              <td className="px-4 py-3 tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">{formatPct(r.model_prob)}</td>
              <td className="px-4 py-3"><EdgeBadge edge={bestEdge(r)} /></td>
              <td className="px-4 py-3 tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">
                {r.volume ? Math.round(r.volume).toLocaleString() : "—"}
              </td>
              <td className="px-4 py-3 text-[var(--color-text-muted)] text-xs max-w-xs">{r.no_baseline_reason ?? "—"}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={11} className="px-4 py-10 text-center text-[var(--color-text-dim)]">
                No Tennis markets tracked yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function Tennis() {
  const marketsQuery = useQuery({ queryKey: ["tennis", "markets"], queryFn: fetchTennisMarkets });

  const rows = marketsQuery.data ?? [];

  const stats = useMemo(() => {
    const matches = new Set(rows.map((r) => r.match_label).filter(Boolean));
    const withEdge = rows.filter((r) => r.edge !== null);
    const avgAbsEdge = withEdge.length ? withEdge.reduce((s, r) => s + Math.abs(r.edge!), 0) / withEdge.length : null;
    return { matchCount: matches.size, marketCount: rows.length, avgAbsEdge };
  }, [rows]);

  return (
    <PageShell title="Tennis">
      {marketsQuery.isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}

      {marketsQuery.isLoading ? (
        <>
          <StatTilesSkeleton />
          <TableSkeleton />
        </>
      ) : (
        <>
          <div className="flex flex-wrap border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] mb-6 divide-x divide-[var(--color-border)]">
            <StatTile label="Matches tracked" value={String(stats.matchCount)} sublabel="ATP/WTA tour, Challenger, ITF -- both platforms" />
            <StatTile label="Market rows" value={String(stats.marketCount)} sublabel="moneyline, set winner, game spread/total, exact score" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="model vs market, absolute"
            />
          </div>

          <p className="text-xs text-[var(--color-text-muted)] mb-4 max-w-2xl">
            Live Tennis moneyline, set winner, game spread/total, and exact match score from Kalshi and
            Polymarket (Kalshi's own game spread/total is ATP-only -- confirmed no WTA equivalent exists
            there; Polymarket lists both), across ATP/WTA tour, Challenger, and ITF level. Moneyline is a
            surface-blended walk-forward
            Elo, backtested fresh in this app's own harness -- the market beat it at every tier tested
            (see Backtests). Set/game markets are derived from real per-set/per-game historical data
            (see Backtests for the derivation) but haven't been backtested against real historical odds
            for these specific market types yet. Everything here ships as an honest reference estimate,
            not a claimed edge (model_validated: false).
          </p>

          <TennisMarketsTable rows={rows} />
        </>
      )}
    </PageShell>
  );
}
