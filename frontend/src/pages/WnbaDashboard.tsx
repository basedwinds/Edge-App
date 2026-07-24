import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { SourceBadge } from "../components/markets/SourceBadge";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchWnbaMarkets } from "../api/markets";
import type { WnbaMarketRow } from "../types/market";

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function formatGameDate(gameday: string | null, gametime: string | null): string {
  if (!gameday) return "—";
  const d = new Date(gameday + "T00:00:00");
  const dateStr = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  if (!gametime || gametime === "00:00") return dateStr;
  const t = new Date(`${gameday}T${gametime}:00Z`); // gametime stored UTC
  const timeStr = t.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${dateStr}, ${timeStr}`;
}

function WnbaMarketsTable({ rows }: { rows: WnbaMarketRow[] }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-x-auto">
      <table className="w-full text-sm border-collapse min-w-[760px]">
        <thead>
          <tr className="border-b border-[var(--color-border)]">
            {["Date", "Game", "Team", "Source", "Market %", "Est. %", "Edge", "Volume", "Note"].map((h) => (
              <th key={h} className="text-left px-4 py-3 text-xs uppercase tracking-wide text-[var(--color-text-dim)] font-medium whitespace-nowrap">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-surface-hover)] transition-colors">
              <td className="px-4 py-3 whitespace-nowrap font-mono text-[var(--color-text-dim)]">{formatGameDate(r.gameday, r.gametime)}</td>
              <td className="px-4 py-3 whitespace-nowrap">{r.game_label ?? "—"}</td>
              <td className="px-4 py-3 font-medium whitespace-nowrap">{r.team ?? "—"}</td>
              <td className="px-4 py-3"><SourceBadge source={r.source} /></td>
              <td className="px-4 py-3 tabular-nums font-mono whitespace-nowrap">{formatPct(r.implied_prob)}</td>
              <td className="px-4 py-3 tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">{formatPct(r.model_prob)}</td>
              <td className="px-4 py-3"><EdgeBadge edge={r.edge} /></td>
              <td className="px-4 py-3 tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">{r.volume ? Math.round(r.volume).toLocaleString() : "—"}</td>
              <td className="px-4 py-3 text-[var(--color-text-muted)] text-xs max-w-xs">{r.no_baseline_reason ?? "—"}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={9} className="px-4 py-10 text-center text-[var(--color-text-dim)]">
                No WNBA game markets tracked right now — likely between games or the All-Star break.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function WnbaDashboard() {
  const marketsQuery = useQuery({ queryKey: ["wnba", "markets"], queryFn: fetchWnbaMarkets });
  const rows = marketsQuery.data ?? [];

  const stats = useMemo(() => {
    const games = new Set(rows.map((r) => r.game_label).filter(Boolean));
    const withEdge = rows.filter((r) => r.edge !== null);
    const avgAbsEdge = withEdge.length ? withEdge.reduce((s, r) => s + Math.abs(r.edge!), 0) / withEdge.length : null;
    return { gameCount: games.size, marketCount: rows.length, avgAbsEdge };
  }, [rows]);

  return (
    <PageShell title="WNBA">
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
            <StatTile label="Games tracked" value={String(stats.gameCount)} sublabel="moneyline only (Kalshi lists no WNBA spread/total/futures)" />
            <StatTile label="Market rows" value={String(stats.marketCount)} sublabel="per team, per source" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="model vs market, absolute"
            />
          </div>

          <p className="text-xs text-[var(--color-text-muted)] mb-4 max-w-2xl">
            WNBA is a moneyline-only integration — an Elo baseline prices each game, but the model matches
            (doesn't beat) the market on average (model_validated: false). Kalshi lists no WNBA spread,
            total, or futures markets, so there's nothing more to price. An empty table usually means the
            league is between games or on the All-Star break, not a bug — the Health Check page will say so.
          </p>

          <WnbaMarketsTable rows={rows} />
        </>
      )}
    </PageShell>
  );
}
