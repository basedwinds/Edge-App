import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { SourceBadge } from "../components/markets/SourceBadge";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchMmaMarkets } from "../api/markets";
import type { MmaMarketRow } from "../types/market";

const MARKET_TYPE_LABELS: Record<string, string> = {
  moneyline: "Moneyline",
  distance: "Goes the Distance",
  method_of_victory: "Method of Victory",
  method_of_finish: "Method of Finish",
  rounds: "Round of Finish",
  round_of_victory: "Round of Victory",
};

const SIDE_LABELS: Record<string, string> = {
  decision: "by Decision",
  kotko: "by KO/TKO",
  submission: "by Submission",
  draw: "Draw/No Contest",
  other: "Decision/Draw/NC",
};

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function bestEdge(r: MmaMarketRow): number | null {
  return r.model_prob !== null && r.implied_prob !== null ? r.model_prob - r.implied_prob : null;
}

// Plain-English outcome description per market type -- same "spell out the
// real pick, don't make the reader decode raw fields" convention as
// RecommendedBetsTable.tsx's describePick across the other sports.
function formatOutcome(r: MmaMarketRow): string {
  switch (r.market_type) {
    case "moneyline":
      return r.team ?? "—";
    case "distance":
      return "Goes the distance";
    case "method_of_victory":
      if (r.side === "draw") return "Draw/No Contest";
      return `${r.team ?? "—"} ${SIDE_LABELS[r.side ?? ""] ?? ""}`.trim();
    case "method_of_finish":
      return SIDE_LABELS[r.side ?? ""] ?? r.side ?? "—";
    case "rounds":
      return r.line !== null ? `Ends before round ${Math.round(r.line)}` : "—";
    case "round_of_victory":
      if (r.side === "other") return "Decision/Draw/NC";
      return r.team && r.line !== null ? `${r.team} wins in round ${Math.round(r.line)}` : (r.team ?? "—");
    default:
      return r.team ?? "—";
  }
}

function formatEventDate(eventDate: string | null): string {
  if (!eventDate) return "—";
  const d = new Date(eventDate + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function MmaMarketsTable({ rows }: { rows: MmaMarketRow[] }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-x-auto">
      <table className="w-full text-sm border-collapse min-w-[900px]">
        <thead>
          <tr className="border-b border-[var(--color-border)]">
            {["Date", "Fight", "Market", "Outcome", "Source", "Market %", "Est. %", "Edge", "Volume", "Note"].map((h) => (
              <th key={h} className="text-left px-4 py-3 text-xs uppercase tracking-wide text-[var(--color-text-dim)] font-medium whitespace-nowrap">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-surface-hover)] transition-colors">
              <td className="px-4 py-3 whitespace-nowrap font-mono text-[var(--color-text-dim)]">{formatEventDate(r.event_date)}</td>
              <td className="px-4 py-3 whitespace-nowrap">
                {r.fight_label ?? "—"}
                {r.model_note && (
                  <span
                    className="ml-2 text-[10px] font-medium text-[var(--color-warning)] cursor-help"
                    title={r.model_note}
                  >
                    ⚠ style/defence
                  </span>
                )}
                {r.is_title_bout && <span className="ml-1.5 text-[10px] uppercase tracking-wide text-[var(--color-accent)]">Title</span>}
              </td>
              <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-dim)]">{MARKET_TYPE_LABELS[r.market_type] ?? r.market_type}</td>
              <td className="px-4 py-3 font-medium whitespace-nowrap">{formatOutcome(r)}</td>
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
              <td colSpan={10} className="px-4 py-10 text-center text-[var(--color-text-dim)]">
                No MMA markets tracked yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function Mma() {
  const marketsQuery = useQuery({ queryKey: ["mma", "markets"], queryFn: fetchMmaMarkets });

  const rows = marketsQuery.data ?? [];

  const stats = useMemo(() => {
    const fights = new Set(rows.map((r) => r.fight_label).filter(Boolean));
    const withEdge = rows.filter((r) => r.edge !== null);
    const avgAbsEdge = withEdge.length ? withEdge.reduce((s, r) => s + Math.abs(r.edge!), 0) / withEdge.length : null;
    return { fightCount: fights.size, marketCount: rows.length, avgAbsEdge };
  }, [rows]);

  return (
    <PageShell title="MMA">
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
            <StatTile label="Fights tracked" value={String(stats.fightCount)} sublabel="upcoming UFC cards, both platforms" />
            <StatTile label="Market rows" value={String(stats.marketCount)} sublabel="moneyline / distance / method / rounds, per source" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="model vs market, absolute -- no baseline built yet"
            />
          </div>

          <p className="text-xs text-[var(--color-text-muted)] mb-4 max-w-2xl">
            Live UFC moneyline, go-the-distance, method-of-victory/finish, and round markets from
            Kalshi and Polymarket. The baseline model is still being built and validated against this
            app's own historical UFC data (not shipped as a guessed number) -- Est. %/Edge will populate
            once that's done.
          </p>

          <MmaMarketsTable rows={rows} />
        </>
      )}
    </PageShell>
  );
}
