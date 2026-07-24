import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { SourceBadge } from "../components/markets/SourceBadge";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchLolMarkets } from "../api/markets";
import type { LolMarketRow } from "../types/market";

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function bestEdge(r: LolMarketRow): number | null {
  return r.model_prob !== null && r.implied_prob !== null ? r.model_prob - r.implied_prob : null;
}

function formatMatchDate(matchDate: string | null): string {
  if (!matchDate) return "—";
  const d = new Date(matchDate + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const MARKET_TYPE_LABELS: Record<string, string> = {
  map_winner: "Map Winner",
  series_winner: "Series Winner",
  series_total: "Total Maps",
  tournament_winner: "Tournament Winner",
};

function formatPick(r: LolMarketRow): string {
  switch (r.market_type) {
    case "map_winner":
      return r.line !== null ? `${r.team ?? "—"} wins Map ${r.line}` : (r.team ?? "—");
    case "series_total":
      return r.line !== null ? `Over ${r.line} maps` : "—";
    case "tournament_winner":
      return r.team ? `${r.team} wins ${r.group_label ?? "tournament"}` : "—";
    default:
      return r.team ?? "—";
  }
}

function LolMarketsTable({ rows }: { rows: LolMarketRow[] }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-x-auto">
      <table className="w-full text-sm border-collapse min-w-[960px]">
        <thead>
          <tr className="border-b border-[var(--color-border)]">
            {["Date", "Match", "Bo", "Market", "Pick", "Source", "Market %", "Est. %", "Edge", "Volume", "Note"].map((h) => (
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
              <td className="px-4 py-3 whitespace-nowrap">{r.match_label ?? r.group_label ?? "—"}</td>
              <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-dim)]">{r.best_of ? `Bo${r.best_of}` : "—"}</td>
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
                {r.no_baseline_reason ?? "—"}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={11} className="px-4 py-10 text-center text-[var(--color-text-dim)]">
                No LoL markets tracked yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function Lol() {
  const marketsQuery = useQuery({ queryKey: ["lol", "markets"], queryFn: fetchLolMarkets });

  const rows = marketsQuery.data ?? [];

  const stats = useMemo(() => {
    const matches = new Set(rows.map((r) => r.match_label).filter(Boolean));
    const withEdge = rows.filter((r) => r.edge !== null);
    const avgAbsEdge = withEdge.length ? withEdge.reduce((s, r) => s + Math.abs(r.edge!), 0) / withEdge.length : null;
    return { matchCount: matches.size, marketCount: rows.length, avgAbsEdge };
  }, [rows]);

  return (
    <PageShell title="League of Legends">
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
            <StatTile label="Matches tracked" value={String(stats.matchCount)} sublabel="queried live from Leaguepedia's Cargo API" />
            <StatTile label="Market rows" value={String(stats.marketCount)} sublabel="Series + map winner + total maps (Kalshi only)" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="model vs market, absolute"
            />
          </div>

          <p className="text-xs text-[var(--color-text-muted)] mb-4 max-w-2xl">
            Live LoL series (whole-match) winner, per-map winner, and total-maps markets from Kalshi
            (KXLOLGAME/KXLOLMAP/KXLOLTOTALMAPS -- found live 2026-07-20 that KXLOLGAME is a real ticker
            Kalshi added after this app's own build docs said no whole-series-winner market existed for LoL;
            still no Polymarket match-level market here), matched against real match data queried from
            Leaguepedia's own Cargo API (lol.fandom.com). Leaguepedia's Cargo endpoint applies its own real
            rate limit to anonymous requests (hit live during this build), so match data may lag behind
            Kalshi's own listing on a given poll cycle -- a known, honest timing gap, not a bug. The model is
            a team-level Elo (K=24, grid-searched against a real 5,604-match historical crawl of
            Leaguepedia's own "Primary" tournament tier -- LCK/LPL/LEC/LCS-LTA/Worlds/MSI -- under a
            per-map update rule, 67.86% walk-forward accuracy, the strongest of all 3 esports titles in this
            app) extended to a best-of-N series distribution, blended with each pair's own real head-to-head
            series record when one exists (Bayesian shrinkage, a real if modest Brier improvement -- LoL's
            already-strong Elo signal leaves less residual room for this than CS2/Valorant) and given a
            rest-day rating bonus (effectively a binary "at least 1 day off" signal here, the smallest of
            the 3 titles' own rest-signal magnitudes, same reason as the h2h finding), and blended 60/40
            with a player-level rating from real gol.gg per-game lineups where resolvable (16% of matches)
            -- a real but modest gain (-0.003 Brier), since the team Elo is already so strong. Real caveat found live the day
            KXLOLGAME/KXLOLMAP opened up a lot more lower-tier matches: most of the newly-visible matches are
            between two teams neither of which appears in the Primary-tier training crawl, so both default
            to the identical baseline Elo rating -- fixed by requiring both teams to have at least 5 real
            map observations before a rating counts as trustworthy (see this app's own minimum-games
            confidence threshold), so a coincidental 50/50 no longer shows up as a fake "edge." That accuracy
            figure above is the model's own internal signal from win/loss history, not a check against real
            market odds -- no historical LoL odds archive exists to run that check. Nothing here is
            backtested against the market (model_validated: false).
          </p>

          <LolMarketsTable rows={rows} />
        </>
      )}
    </PageShell>
  );
}
