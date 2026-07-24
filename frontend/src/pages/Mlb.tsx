import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { SourceBadge } from "../components/markets/SourceBadge";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchMlbMarkets } from "../api/markets";
import type { MlbMarketRow } from "../types/market";

const MARKET_TYPE_LABELS: Record<string, string> = {
  moneyline: "Moneyline",
  spread: "Run Line",
  total: "Total",
  team_total: "Team Total",
  f5: "First 5 Innings",
  rfi: "Run in 1st Inning",
};

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

// Prefers final_prob (baseline + news, when a news adjustment is cached) as
// the "current best estimate" for DISPLAY here, same convention as NFL's
// Dashboard (see markets.ts::groupByGameAndSource) -- equals model_prob when
// there's no news on file, so this is a strict superset, not a behavior
// change. Recommended Bets' own suggested stakes are unaffected by this --
// they're computed server-side from model_prob for every sport, a separate,
// deliberately more conservative convention this display-only helper
// doesn't touch.
function bestEdge(r: MlbMarketRow): number | null {
  const best = r.final_prob ?? r.model_prob;
  return best !== null && r.implied_prob !== null ? best - r.implied_prob : null;
}

// Same plain-English convention as RecommendedBetsTable.tsx's describePick --
// this table's raw signed `line` for a spread/run-line is "this team's OWN
// margin must exceed line" (game_lines_mlb.py::prob_team_covers), which reads
// backwards from standard bookmaker notation, so spell out the real
// threshold instead of showing the signed number alone.
function formatLine(r: MlbMarketRow): string {
  if (r.market_type === "f5") return r.side === "tie" ? "Tie after 5" : `${r.team ?? "—"} wins F5`;
  if (r.market_type === "rfi") return r.side === "no" ? "No run in 1st" : "Run in 1st";
  if (r.line === null) return "—";
  if (r.market_type === "total" || r.market_type === "team_total") return `${r.side === "under" ? "Under" : "Over"} ${r.line}`;
  if (r.market_type === "spread") {
    if (!r.team) return `${r.line > 0 ? "+" : ""}${r.line}`;
    if (r.line > 0) return `${r.team} wins by ${Math.ceil(r.line)}+`;
    if (r.line < 0) return `${r.team} doesn't lose by ${Math.ceil(Math.abs(r.line))}+`;
    return `${r.team} wins outright`;
  }
  return `${r.line > 0 ? "+" : ""}${r.line}`;
}

// gametime is date-only-safe but NOT time-safe for MLB -- gameday/gametime
// don't reconstruct a correct kickoff instant for evening games that cross
// the UTC day boundary (see pitcher_ratings_mlb.py-adjacent notes / mlb_data.py
// docstring), so this shows the date only rather than risk a wrong time.
function formatGameDate(gameday: string | null): string {
  if (!gameday) return "—";
  const d = new Date(gameday + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function MlbGameMarketsTable({ rows }: { rows: MlbMarketRow[] }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-x-auto">
      <table className="w-full text-sm border-collapse min-w-[960px]">
        <thead>
          <tr className="border-b border-[var(--color-border)]">
            {["Date", "Game", "Probable SPs", "Market", "Team", "Line", "Source", "Market %", "Est. %", "Edge", "Volume", "Note"].map((h) => (
              <th key={h} className="text-left px-4 py-3 text-xs uppercase tracking-wide text-[var(--color-text-dim)] font-medium whitespace-nowrap">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-surface-hover)] transition-colors">
              <td className="px-4 py-3 whitespace-nowrap font-mono text-[var(--color-text-dim)]">{formatGameDate(r.gameday)}</td>
              <td className="px-4 py-3 whitespace-nowrap">{r.game_label ?? "—"}</td>
              <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-dim)] text-xs">
                {r.away_probable_pitcher || r.home_probable_pitcher
                  ? `${r.away_probable_pitcher ?? "TBD"} @ ${r.home_probable_pitcher ?? "TBD"}`
                  : "—"}
              </td>
              <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-dim)]">{MARKET_TYPE_LABELS[r.market_type] ?? r.market_type}</td>
              <td className="px-4 py-3 font-medium whitespace-nowrap">{r.team ?? "—"}</td>
              <td className="px-4 py-3 tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">{formatLine(r)}</td>
              <td className="px-4 py-3"><SourceBadge source={r.source} /></td>
              <td className="px-4 py-3 tabular-nums font-mono whitespace-nowrap">{formatPct(r.implied_prob)}</td>
              <td className="px-4 py-3 tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">
                <span className="inline-flex items-center gap-1">
                  {formatPct(r.final_prob ?? r.model_prob)}
                  {r.news_adjustment_pct !== null && (
                    <span title="Includes a news/research adjustment" className="text-[var(--color-good)]">•</span>
                  )}
                </span>
              </td>
              <td className="px-4 py-3"><EdgeBadge edge={bestEdge(r)} /></td>
              <td className="px-4 py-3 tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">
                {r.volume ? Math.round(r.volume).toLocaleString() : "—"}
              </td>
              <td className="px-4 py-3 text-[var(--color-text-muted)] text-xs max-w-xs">{r.no_baseline_reason ?? "—"}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={12} className="px-4 py-10 text-center text-[var(--color-text-dim)]">
                No MLB game markets tracked yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function Mlb() {
  const marketsQuery = useQuery({ queryKey: ["mlb", "markets"], queryFn: fetchMlbMarkets });

  const rows = marketsQuery.data ?? [];

  const stats = useMemo(() => {
    const games = new Set(rows.map((r) => r.game_label).filter(Boolean));
    const withEdge = rows.filter((r) => r.edge !== null);
    const avgAbsEdge = withEdge.length ? withEdge.reduce((s, r) => s + Math.abs(r.edge!), 0) / withEdge.length : null;
    return { gameCount: games.size, marketCount: rows.length, avgAbsEdge };
  }, [rows]);

  return (
    <PageShell title="MLB">
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
            <StatTile label="Games tracked" value={String(stats.gameCount)} sublabel="mid-season -- moneyline/run-line/total/team-total all open now" />
            <StatTile label="Game market rows" value={String(stats.marketCount)} sublabel="moneyline / run line / total / team total, per team, per source" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="model vs market, absolute"
            />
          </div>

          <p className="text-xs text-[var(--color-text-muted)] mb-4 max-w-2xl">
            Moneyline and run line are priced by a team-Elo model blended with a starting-pitcher signal
            (validated against 10 years of real historical data -- the blend beats team-Elo-alone on
            real outcomes, same discipline as the NFL/NBA models). Total and team total use
            league-average runs adjusted for a real, derived ballpark factor (e.g. Coors Field runs well
            above average) -- team-scoring-rate and starting-pitcher-ERA signals were checked and
            confirmed NOT to help for MLB totals, unlike NFL/NBA, but the ballpark effect is real and
            does (see the Note column and Backtests for details).
          </p>

          <MlbGameMarketsTable rows={rows} />
        </>
      )}
    </PageShell>
  );
}
