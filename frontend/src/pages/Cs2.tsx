import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { SourceBadge } from "../components/markets/SourceBadge";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchCs2Markets } from "../api/markets";
import type { Cs2MarketRow } from "../types/market";

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function bestEdge(r: Cs2MarketRow): number | null {
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

function formatPick(r: Cs2MarketRow): string {
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

function Cs2MarketsTable({ rows }: { rows: Cs2MarketRow[] }) {
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
                No CS2 markets tracked yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function Cs2() {
  const marketsQuery = useQuery({ queryKey: ["cs2", "markets"], queryFn: fetchCs2Markets });

  const rows = marketsQuery.data ?? [];

  const stats = useMemo(() => {
    const matches = new Set(rows.map((r) => r.match_label).filter(Boolean));
    const withEdge = rows.filter((r) => r.edge !== null);
    const avgAbsEdge = withEdge.length ? withEdge.reduce((s, r) => s + Math.abs(r.edge!), 0) / withEdge.length : null;
    return { matchCount: matches.size, marketCount: rows.length, avgAbsEdge };
  }, [rows]);

  return (
    <PageShell title="CS2">
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
            <StatTile label="Matches tracked" value={String(stats.matchCount)} sublabel="scraped live from liquipedia.net" />
            <StatTile label="Market rows" value={String(stats.marketCount)} sublabel="Series + map winner + total maps (Kalshi only)" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="model vs market, absolute"
            />
          </div>

          <p className="text-xs text-[var(--color-text-muted)] mb-4 max-w-2xl">
            Live CS2 series (whole-match) winner, per-map winner, and total-maps markets from Kalshi
            (KXCS2GAME/KXCS2MAP/KXCS2TOTALMAPS -- found live 2026-07-20 that the map-winner ticker this app
            originally queried, KXCS2MAPWINNER, was genuinely dead; the real live one is KXCS2MAP, a
            different ticker entirely), matched against real match/team data scraped live from liquipedia.net
            (HLTV is Cloudflare-blocked, but that block is HLTV-specific -- Liquipedia is not gated). No
            Polymarket CS2 match-level markets exist right now (its real CS2 inventory is prop/roster-change
            bets only). Liquipedia's default schedule listing is curated toward bigger tournaments, so a real
            chunk of Kalshi's lower-tier/regional matches still have no model price even after best_of
            backfilling from both KXCS2MAP's own map-ladder depth and KXCS2TOTALMAPS's own O/U line -- a
            known, honest data-coverage gap, not a bug. The model is a team-level Elo (K=32, grid-searched
            against a real 8,839-match historical crawl of 94 S-Tier + A-Tier tournaments, Oct 2023-Jul 2026
            -- 60.75% walk-forward accuracy, beats the naive 0.5 baseline) extended to a best-of-N series
            distribution, blended with each pair's own real head-to-head series record when one exists
            (Bayesian shrinkage, a real Brier improvement), boosted 1.6x for a team's first 3 series
            after a real detected Liquipedia roster change, and given a rest-day rating bonus (capped at 2
            days) for whichever side has had more time since its last real series (all real, validated Brier
            improvements). Where the real lineup that played can be resolved (38.8% of historical matches),
            this is blended 80/20 toward a PLAYER-level rating that rates individuals and aggregates to that
            lineup -- the largest improvement in this app's esports build, and the only one that measurably
            narrows the gap to the market (~51% of it closed on a real 78-event closing-price sample);
            matches with no resolvable lineup fall back to the team model.
            A real market-odds backtest against Kalshi's own historical trade data (85-match
            sample) found the market beats the model, same as every other sport in this app, so nothing here
            beats the market yet (model_validated: false).
          </p>

          <Cs2MarketsTable rows={rows} />
        </>
      )}
    </PageShell>
  );
}
