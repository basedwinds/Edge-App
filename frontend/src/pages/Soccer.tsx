import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { SourceBadge } from "../components/markets/SourceBadge";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchSoccerMarkets } from "../api/markets";
import type { SoccerMarketRow } from "../types/market";

const LEAGUE_LABELS: Record<string, string> = {
  E0: "EPL",
  SP1: "La Liga",
  I1: "Serie A",
  D1: "Bundesliga",
  F1: "Ligue 1",
  MLS: "MLS",
};

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function bestEdge(r: SoccerMarketRow): number | null {
  return r.model_prob !== null && r.implied_prob !== null ? r.model_prob - r.implied_prob : null;
}

function formatMatchDate(matchDate: string | null): string {
  if (!matchDate) return "—";
  const d = new Date(matchDate + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const MARKET_TYPE_LABELS: Record<string, string> = {
  moneyline_3way: "Moneyline",
  game_spread: "Spread",
  game_total: "Total",
  btts: "BTTS",
  team_total: "Team Total",
  ftts: "1st To Score",
  correct_score: "Correct Score",
  first_half_winner: "1H Winner",
  first_half_spread: "1H Spread",
  first_half_total: "1H Total",
  first_half_team_total: "1H Team Total",
  first_half_btts: "1H BTTS",
  second_half_winner: "2H Winner",
  second_half_spread: "2H Spread",
  second_half_total: "2H Total",
  second_half_team_total: "2H Team Total",
  second_half_btts: "2H BTTS",
};

// Spells out exactly what each row's pick means -- same "don't make the
// reader decode raw fields" convention as Tennis.tsx's formatPick.
function formatPick(r: SoccerMarketRow): string {
  switch (r.market_type) {
    case "game_spread":
    case "first_half_spread":
    case "second_half_spread":
      return r.line !== null ? `${r.team ?? "—"} wins by ${r.line}+ goals` : (r.team ?? "—");
    case "game_total":
    case "first_half_total":
    case "second_half_total":
      return r.line !== null ? `Over ${r.line} goals` : "—";
    case "team_total":
    case "first_half_team_total":
    case "second_half_team_total":
      return r.line !== null ? `${r.team ?? "—"} over ${r.line} goals` : (r.team ?? "—");
    case "btts":
    case "first_half_btts":
    case "second_half_btts":
      return "Both teams to score";
    case "ftts":
      return r.side === "none" ? "Neither team scores" : (r.team ?? "—");
    case "correct_score":
      return r.correct_score_home !== null && r.correct_score_away !== null
        ? `${r.correct_score_home} - ${r.correct_score_away}`
        : "—";
    case "first_half_winner":
    case "second_half_winner":
      return r.side === "draw" ? "Draw" : (r.team ?? "—");
    default:
      if (r.side === "draw") return "Draw";
      return r.team ?? "—";
  }
}

function SoccerMarketsTable({ rows }: { rows: SoccerMarketRow[] }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-x-auto">
      <table className="w-full text-sm border-collapse min-w-[960px]">
        <thead>
          <tr className="border-b border-[var(--color-border)]">
            {["Date", "Match", "League", "Market", "Pick", "Source", "Market %", "Est. %", "Edge", "Volume", "Note"].map((h) => (
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
              <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-dim)]">{LEAGUE_LABELS[r.league ?? ""] ?? r.league ?? "—"}</td>
              <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-dim)]">{MARKET_TYPE_LABELS[r.market_type] ?? r.market_type}</td>
              <td className="px-4 py-3 font-medium whitespace-nowrap">{formatPick(r)}</td>
              <td className="px-4 py-3"><SourceBadge source={r.source} /></td>
              <td className="px-4 py-3 tabular-nums font-mono whitespace-nowrap">{formatPct(r.implied_prob)}</td>
              <td className="px-4 py-3 tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">{formatPct(r.model_prob)}</td>
              <td className="px-4 py-3"><EdgeBadge edge={bestEdge(r)} /></td>
              <td className="px-4 py-3 tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">
                {r.volume ? Math.round(r.volume).toLocaleString() : "—"}
              </td>
              <td className="px-4 py-3 text-[var(--color-text-muted)] text-xs max-w-xs">
                {r.no_baseline_reason
                  ?? (r.news_adjustment_pct !== null
                    ? `Injury/motivation blend: ${r.news_adjustment_pct > 0 ? "+" : ""}${r.news_adjustment_pct.toFixed(1)}pp (home)`
                    : "—")}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={11} className="px-4 py-10 text-center text-[var(--color-text-dim)]">
                No Soccer markets tracked yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function Soccer() {
  const marketsQuery = useQuery({ queryKey: ["soccer", "markets"], queryFn: fetchSoccerMarkets });

  const rows = marketsQuery.data ?? [];

  const stats = useMemo(() => {
    const matches = new Set(rows.map((r) => r.match_label).filter(Boolean));
    const withEdge = rows.filter((r) => r.edge !== null);
    const avgAbsEdge = withEdge.length ? withEdge.reduce((s, r) => s + Math.abs(r.edge!), 0) / withEdge.length : null;
    return { matchCount: matches.size, marketCount: rows.length, avgAbsEdge };
  }, [rows]);

  return (
    <PageShell title="Soccer">
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
            <StatTile label="Matches tracked" value={String(stats.matchCount)} sublabel="EPL/La Liga/Serie A/Bundesliga/Ligue 1/MLS -- both platforms" />
            <StatTile label="Market rows" value={String(stats.marketCount)} sublabel="3-way moneyline, spread, and total" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="model vs market, absolute"
            />
          </div>

          <p className="text-xs text-[var(--color-text-muted)] mb-4 max-w-2xl">
            Live Soccer 3-way moneyline (Home/Draw/Away), goal spread ("wins by more than N goals"), and
            total goals (Over/Under) from Kalshi and Polymarket, across EPL, La Liga, Serie A, Bundesliga,
            Ligue 1, and MLS. The model is a walk-forward attack/defense Poisson goal rating (see
            Backtests), backtested fresh in this app's own harness against real historical odds for the 5
            European leagues -- the market beat it in every league tested, for all three market types. MLS
            has no free historical-odds source at all, so its ratings are real (fit from ESPN's own match
            results) but can never be backtested against a market baseline. Everything here ships as an
            honest reference estimate, not a claimed edge (model_validated: false).
          </p>

          <SoccerMarketsTable rows={rows} />
        </>
      )}
    </PageShell>
  );
}
