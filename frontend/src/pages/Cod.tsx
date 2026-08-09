import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { SourceBadge } from "../components/markets/SourceBadge";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchCodMarkets } from "../api/markets";
import type { CodMarketRow } from "../types/market";

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function formatMatchDate(matchDate: string | null): string {
  if (!matchDate) return "—";
  const d = new Date(matchDate + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const MARKET_TYPE_LABELS: Record<string, string> = {
  series_winner: "Match Winner",
};

function CodMarketsTable({ rows }: { rows: CodMarketRow[] }) {
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
              <td className="px-4 py-3 whitespace-nowrap">
                {r.match_label ?? "—"}
                {/* The live badge is shown, not hidden: a refused row with no
                    explanation reads as the app being broken. */}
                {r.is_live && (
                  <span className="ml-2 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide bg-[var(--color-critical)]/15 text-[var(--color-critical)] align-middle">
                    Live
                  </span>
                )}
              </td>
              <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-dim)]">{r.best_of ? `Bo${r.best_of}` : "—"}</td>
              <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-dim)]">{MARKET_TYPE_LABELS[r.market_type] ?? r.market_type}</td>
              <td className="px-4 py-3 font-medium whitespace-nowrap">{r.team ?? "—"}</td>
              <td className="px-4 py-3"><SourceBadge source={r.source} /></td>
              <td className="px-4 py-3 tabular-nums font-mono whitespace-nowrap">{formatPct(r.implied_prob)}</td>
              <td className="px-4 py-3 tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">{formatPct(r.model_prob)}</td>
              <td className="px-4 py-3"><EdgeBadge edge={r.edge} /></td>
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
                No Call of Duty markets tracked yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function Cod() {
  const marketsQuery = useQuery({ queryKey: ["cod", "markets"], queryFn: fetchCodMarkets });

  const rows = marketsQuery.data ?? [];

  const stats = useMemo(() => {
    const matches = new Set(rows.map((r) => r.match_label).filter(Boolean));
    const withEdge = rows.filter((r) => r.edge !== null);
    const avgAbsEdge = withEdge.length ? withEdge.reduce((s, r) => s + Math.abs(r.edge!), 0) / withEdge.length : null;
    const live = rows.filter((r) => r.is_live).length;
    return { matchCount: matches.size, marketCount: rows.length, avgAbsEdge, live };
  }, [rows]);

  return (
    <PageShell title="Call of Duty">
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
            <StatTile label="Matches tracked" value={String(stats.matchCount)} sublabel="from breakingpoint.gg" />
            <StatTile label="Market rows" value={String(stats.marketCount)} sublabel="Match winner (Kalshi KXCODGAME)" />
            <StatTile label="In progress" value={String(stats.live)} sublabel="live matches, never priced" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="model vs market, absolute"
            />
          </div>

          <p className="text-xs text-[var(--color-text-muted)] mb-4 max-w-2xl">
            Live Call of Duty match-winner markets from Kalshi (KXCODGAME), matched against real match data
            from breakingpoint.gg. Kalshi lists no CoD spread, totals, per-map or futures markets, so match
            winner is the whole board here — that is the real inventory, not a coverage gap.
            The model is a team-level Elo trained on 3,615 real matches (2020–2026), scoring 64.8%
            walk-forward accuracy over 2,508 predictions — between this app&rsquo;s CS2 (60.8%) and LoL
            (67.1%) models, and flat across K=16–40 so it is not a tuning artefact. It has NO player-level,
            roster-change or map-pool layer, because breakingpoint publishes no lineups or transfers to
            build one from; the other titles have those only because their sources do.
            A match that is ALREADY IN PROGRESS is never priced: breakingpoint reports match status
            directly, so this is refused on a real live flag rather than by comparing a platform start time
            against the clock. That distinction is not theoretical — the first CoD market found while
            building this was live at 2–0 while Kalshi&rsquo;s own start time still claimed it was four hours
            away, and a pre-match model against a live price showed a phantom 31pp &ldquo;edge&rdquo;.
            No backtest against real CoD market odds has been run yet, so nothing here is claimed to beat
            the market (model_validated: false), and its bankroll allocation is deliberately set below the
            established sports&rsquo;.
          </p>

          <CodMarketsTable rows={rows} />
        </>
      )}
    </PageShell>
  );
}
