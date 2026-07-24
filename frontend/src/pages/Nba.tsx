import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { SourceBadge } from "../components/markets/SourceBadge";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchNbaMarkets } from "../api/markets";
import type { NbaMarketRow } from "../types/market";

const MARKET_TYPE_LABELS: Record<string, string> = {
  moneyline: "Moneyline",
  spread: "Spread",
  total: "Total",
  team_total: "Team Total",
  spread_1h: "1H Spread",
  spread_2h: "2H Spread",
  total_1h: "1H Total",
  total_2h: "2H Total",
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
function bestEdge(r: NbaMarketRow): number | null {
  const best = r.final_prob ?? r.model_prob;
  return best !== null && r.implied_prob !== null ? best - r.implied_prob : null;
}

// Same plain-English convention as RecommendedBetsTable.tsx's describePick --
// raw signed `line` for a spread is "this team's OWN margin must exceed
// line" (game_lines_nba.py::prob_team_covers), backwards from standard
// bookmaker notation, so spell out the real threshold instead. Also fixes a
// pre-existing bug where `market_type.startsWith("total")` silently missed
// "team_total" (doesn't start with "total") and showed it as a raw signed
// number instead of Over/Under.
function formatLine(r: NbaMarketRow): string {
  if (r.line === null) return "—";
  if (r.market_type.startsWith("spread")) {
    if (!r.team) return `${r.line > 0 ? "+" : ""}${r.line}`;
    if (r.line > 0) return `${r.team} wins by ${Math.ceil(r.line)}+`;
    if (r.line < 0) return `${r.team} doesn't lose by ${Math.ceil(Math.abs(r.line))}+`;
    return `${r.team} wins outright`;
  }
  return `${r.side === "under" ? "Under" : "Over"} ${r.line}`;
}

function formatGameDate(gameday: string | null, gametime: string | null): string {
  if (!gameday) return "—";
  const d = new Date(gameday + "T00:00:00");
  const dateStr = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  if (!gametime || gametime === "00:00") return dateStr;
  // NBA's gametime is stored UTC (see RecommendedBetsTable.tsx's formatGameDate) --
  // appending "Z" converts to the viewer's own local time.
  const t = new Date(`${gameday}T${gametime}:00Z`);
  const timeStr = t.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${dateStr}, ${timeStr}`;
}

function NbaGameMarketsTable({ rows }: { rows: NbaMarketRow[] }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-x-auto">
      <table className="w-full text-sm border-collapse min-w-[860px]">
        <thead>
          <tr className="border-b border-[var(--color-border)]">
            {["Date", "Game", "Market", "Team", "Line", "Source", "Market %", "Est. %", "Edge", "Volume", "Note"].map((h) => (
              <th key={h} className="text-left px-4 py-3 text-xs uppercase tracking-wide text-[var(--color-text-dim)] font-medium whitespace-nowrap">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-surface-hover)] transition-colors">
              <td className="px-4 py-3 whitespace-nowrap font-mono text-[var(--color-text-dim)]">{formatGameDate(r.gameday, r.gametime)}</td>
              <td className="px-4 py-3 whitespace-nowrap">{r.game_label ?? "—"}</td>
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
              <td colSpan={11} className="px-4 py-10 text-center text-[var(--color-text-dim)]">
                No NBA game markets tracked yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function Nba() {
  const marketsQuery = useQuery({ queryKey: ["nba", "markets"], queryFn: fetchNbaMarkets });

  const rows = marketsQuery.data ?? [];

  const stats = useMemo(() => {
    const games = new Set(rows.map((r) => r.game_label).filter(Boolean));
    const withEdge = rows.filter((r) => r.edge !== null);
    const avgAbsEdge = withEdge.length ? withEdge.reduce((s, r) => s + Math.abs(r.edge!), 0) / withEdge.length : null;
    return { gameCount: games.size, marketCount: rows.length, avgAbsEdge };
  }, [rows]);

  return (
    <PageShell title="NBA">
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
            <StatTile label="Games tracked" value={String(stats.gameCount)} sublabel="Summer League right now -- regular season starts October" />
            <StatTile label="Game market rows" value={String(stats.marketCount)} sublabel="moneyline / spread / total, per team, per source" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="model vs market, absolute -- currently 0 games have a baseline"
            />
          </div>

          <p className="text-xs text-[var(--color-text-muted)] mb-4 max-w-2xl">
            An Elo baseline (validated against 12+ years of real historical data, same discipline as the
            NFL model) now prices moneyline/spread/total -- but every currently-tracked game is Summer
            League, which is deliberately excluded (backup/two-way/rookie rosters, not a fair team-strength
            test), same reasoning as NFL preseason. Est. %/Edge will populate once real regular-season
            inventory opens.
          </p>

          <NbaGameMarketsTable rows={rows} />
        </>
      )}
    </PageShell>
  );
}
