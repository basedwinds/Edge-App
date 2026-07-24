import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { SourceBadge } from "../components/markets/SourceBadge";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchValorantMarkets } from "../api/markets";
import type { ValorantMarketRow } from "../types/market";

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function bestEdge(r: ValorantMarketRow): number | null {
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
  series_handicap: "Map Handicap",
  series_total: "Total Maps",
  tournament_winner: "Tournament Winner",
};

// Spells out exactly what each row's pick means -- same "don't make the
// reader decode raw fields" convention as Soccer.tsx's formatPick.
function formatPick(r: ValorantMarketRow): string {
  switch (r.market_type) {
    case "map_winner":
      return r.line !== null ? `${r.team ?? "—"} wins Map ${r.line}` : (r.team ?? "—");
    case "series_handicap":
      return r.line !== null ? `${r.team ?? "—"} ${r.line > 0 ? "+" : ""}${r.line} maps` : (r.team ?? "—");
    case "series_total":
      return r.line !== null ? `Over ${r.line} maps` : "—";
    case "tournament_winner":
      return r.team ? `${r.team} wins ${r.group_label ?? "tournament"}` : "—";
    default:
      return r.team ?? "—";
  }
}

function ValorantMarketsTable({ rows }: { rows: ValorantMarketRow[] }) {
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
                No Valorant markets tracked yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function Valorant() {
  const marketsQuery = useQuery({ queryKey: ["valorant", "markets"], queryFn: fetchValorantMarkets });

  const rows = marketsQuery.data ?? [];

  const stats = useMemo(() => {
    const matches = new Set(rows.map((r) => r.match_label).filter(Boolean));
    const withEdge = rows.filter((r) => r.edge !== null);
    const avgAbsEdge = withEdge.length ? withEdge.reduce((s, r) => s + Math.abs(r.edge!), 0) / withEdge.length : null;
    return { matchCount: matches.size, marketCount: rows.length, avgAbsEdge };
  }, [rows]);

  return (
    <PageShell title="Valorant">
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
            <StatTile label="Matches tracked" value={String(stats.matchCount)} sublabel="VCT + regional circuits, scraped live from vlr.gg" />
            <StatTile label="Market rows" value={String(stats.marketCount)} sublabel="Map winner, series winner, map handicap, total maps, tournament winner" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="model vs market, absolute"
            />
          </div>

          <p className="text-xs text-[var(--color-text-muted)] mb-4 max-w-2xl">
            Live Valorant map winner, series (match) winner, map handicap, and total-maps markets from
            Kalshi and Polymarket, matched against real match/team data scraped live from vlr.gg (no
            official API exists, and no Cloudflare/bot gate blocks vlr.gg the way it blocks HLTV for CS2).
            The model is a team-level Elo (K=36, grid-searched against a real 19,644-match historical crawl
            of the main VCT circuit + Game Changers + Challengers League, 455 curated events, under a
            per-map update rule -- 63.38% walk-forward accuracy, beats the naive 0.5 baseline) extended to
            a full best-of-N series distribution, blended with each pair's own real head-to-head series
            record when one exists (Bayesian shrinkage, a real Brier improvement) and given a rest-day
            rating bonus (capped at 4 days) for whichever side has had more time since its last real series
            (also a real, validated Brier improvement). A player-level rating (real per-match vlr.gg
            lineups) is blended in at 40% where both lineups are known -- but far weaker here than for CS2
            (-0.001 vs -0.008 Brier), and not market-validatable at Valorant's 19-event closing-price
            sample; the player pool is spread thin across Challengers/Game Changers, and Elo cannot
            calibrate between tiers that rarely meet, so those ratings measure within-tier dominance rather
            than absolute skill. A real market-odds backtest against Kalshi's own
            historical trade data (Map 1 only, 18-match sample) found the market beats the model, same as
            every other sport in this app. Tournament winner futures ship with no model at all yet. Nothing
            here beats the market yet (model_validated: false).
          </p>

          <ValorantMarketsTable rows={rows} />
        </>
      )}
    </PageShell>
  );
}
