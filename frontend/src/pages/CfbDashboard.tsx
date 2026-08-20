import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { SourceBadge } from "../components/markets/SourceBadge";
import { EdgeBadge } from "../components/markets/EdgeBadge";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchCfbMarkets } from "../api/markets";
import type { CfbMarketRow } from "../types/market";
import { teamLabel } from "../utils/pickLabel";

/** Human labels for CFB's eight market types. CFB is unusual in this app --
 *  944 of its 974 markets are season-long -- so the table has to say WHICH
 *  season question a row is asking, not just show a team name. */
const MARKET_TYPE_LABEL: Record<CfbMarketRow["market_type"], string> = {
  moneyline: "Game winner",
  win_total: "Season wins",
  conference_champion: "Conference title",
  conference_qualifier: "Title game",
  conference_regtop: "Conf. finish",
  cfb_playoff: "Playoff",
  cfb_quarterfinal: "Quarterfinal",
  cfb_title_conference: "Natl. title (conf.)",
  // Polymarket-only bracket rounds (added 2026-08-07).
  cfb_top4_seed: "Top-4 Seed",
  cfb_semifinal: "Semifinal",
  cfb_finalist: "Natl. title game",
  cfb_national_champion: "National Champion",
};

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function formatGameDate(gameday: string | null, gametime: string | null): string {
  if (!gameday) return "Season";
  const d = new Date(gameday + "T00:00:00");
  const dateStr = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  if (!gametime || gametime === "00:00") return dateStr;
  const t = new Date(`${gameday}T${gametime}:00Z`);
  return `${dateStr}, ${t.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
}

/** What a season-long row is actually asking, e.g. "9+ wins" / "top 3". */
function describeLine(r: CfbMarketRow): string {
  if (r.line === null) return r.market_type === "moneyline" ? "—" : "Yes";
  if (r.market_type === "win_total") return `${r.line}+ wins`;
  if (r.market_type === "conference_regtop") return `Top ${r.line}`;
  return String(r.line);
}

function CfbMarketsTable({ rows }: { rows: CfbMarketRow[] }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-x-auto">
      <table className="w-full text-sm border-collapse min-w-[900px]">
        <thead>
          <tr className="border-b border-[var(--color-border)]">
            {["Date", "Market", "Team", "Line", "Source", "Market %", "Est. %", "Edge", "Stake", "Note"].map((h) => (
              <th key={h} className="text-left px-4 py-3 text-xs uppercase tracking-wide text-[var(--color-text-dim)] font-medium whitespace-nowrap">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-surface-hover)] transition-colors">
              <td className="px-4 py-3 whitespace-nowrap font-mono text-[var(--color-text-dim)]">{formatGameDate(r.gameday, r.gametime)}</td>
              <td className="px-4 py-3 whitespace-nowrap">
                <span className="text-[var(--color-text-dim)]">{MARKET_TYPE_LABEL[r.market_type]}</span>
                {r.model_approximate && (
                  <span
                    className="ml-2 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide bg-[var(--color-warning)]/15 text-[var(--color-warning)]"
                    title="Priced from a stand-in for the CFP selection committee, and a four-round bracket compounds the rating spread. Staked anyway so forward CLV can judge it — hiding it would make the approximation unfalsifiable."
                  >
                    approx
                  </span>
                )}
              </td>
              {/* Full school name, not Kalshi's code: this column is where
                  "Ohio" was indistinguishable from Ohio State (user-reported
                  2026-08-20). teamLabel falls back to the raw code when a
                  school cannot be resolved with evidence. */}
              <td className="px-4 py-3 font-medium whitespace-nowrap">{teamLabel(r.team, "cfb") ?? "—"}</td>
              <td className="px-4 py-3 whitespace-nowrap text-[var(--color-text-dim)]">{describeLine(r)}</td>
              <td className="px-4 py-3"><SourceBadge source={r.source} /></td>
              <td className="px-4 py-3 tabular-nums font-mono whitespace-nowrap">{formatPct(r.implied_prob)}</td>
              <td className="px-4 py-3 tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">{formatPct(r.model_prob)}</td>
              <td className="px-4 py-3"><EdgeBadge edge={r.edge} /></td>
              <td className="px-4 py-3 tabular-nums font-mono whitespace-nowrap">
                {r.suggested_stake_dollars ? `$${r.suggested_stake_dollars.toFixed(0)}` : "—"}
              </td>
              <td className="px-4 py-3 text-[var(--color-text-muted)] text-xs max-w-xs">{r.no_baseline_reason ?? "—"}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={10} className="px-4 py-10 text-center text-[var(--color-text-dim)]">
                No college football markets match this filter.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function CfbDashboard() {
  const marketsQuery = useQuery({ queryKey: ["cfb", "markets"], queryFn: fetchCfbMarkets });
  const rows = marketsQuery.data ?? [];
  const [typeFilter, setTypeFilter] = useState<string>("all");

  const stats = useMemo(() => {
    const withEdge = rows.filter((r) => r.edge !== null);
    const avgAbsEdge = withEdge.length ? withEdge.reduce((s, r) => s + Math.abs(r.edge!), 0) / withEdge.length : null;
    const priced = rows.filter((r) => r.model_prob !== null).length;
    const games = new Set(rows.filter((r) => r.market_type === "moneyline").map((r) => r.game_label)).size;
    return { marketCount: rows.length, priced, games, avgAbsEdge };
  }, [rows]);

  const byType = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of rows) counts.set(r.market_type, (counts.get(r.market_type) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [rows]);

  const shown = useMemo(
    () => (typeFilter === "all" ? rows : rows.filter((r) => r.market_type === typeFilter)),
    [rows, typeFilter],
  );

  return (
    <PageShell title="College Football">
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
            <StatTile label="Markets" value={String(stats.marketCount)} sublabel="mostly season-long, not per-game" />
            <StatTile label="Priced" value={`${stats.priced}/${stats.marketCount}`} sublabel="model probability available" />
            <StatTile label="Games listed" value={String(stats.games)} sublabel="moneyline fixtures" />
            <StatTile
              label="Avg. disagreement"
              value={stats.avgAbsEdge !== null ? `${(stats.avgAbsEdge * 100).toFixed(1)}pp` : "—"}
              sublabel="model vs market, absolute"
            />
          </div>

          <div className="flex flex-wrap gap-2 mb-4">
            <button
              onClick={() => setTypeFilter("all")}
              className={`rounded px-3 py-1.5 text-xs border transition-colors ${
                typeFilter === "all"
                  ? "border-[var(--color-accent)] text-[var(--color-accent)]"
                  : "border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)]"
              }`}
            >
              All ({rows.length})
            </button>
            {byType.map(([t, n]) => (
              <button
                key={t}
                onClick={() => setTypeFilter(t)}
                className={`rounded px-3 py-1.5 text-xs border transition-colors ${
                  typeFilter === t
                    ? "border-[var(--color-accent)] text-[var(--color-accent)]"
                    : "border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)]"
                }`}
              >
                {MARKET_TYPE_LABEL[t as CfbMarketRow["market_type"]] ?? t} ({n})
              </button>
            ))}
          </div>

          <p className="text-xs text-[var(--color-text-muted)] mb-4 max-w-3xl">
            College football is futures-dominated — 944 of its markets are season-long, so most rows ask a
            season question rather than naming a game. Everything is priced by an Elo derived from 4,836 FBS
            games and confirmed on held-out 2024 and 2025 seasons, but it has <strong>never been scored
            against the market</strong>: Kalshi has no settled CFB game markets to backtest against, so
            model_validated stays false. Rows badged <em>approx</em> are seeded off a stand-in for the
            playoff selection committee — they are staked anyway, deliberately, because suppressing them
            would stop forward CLV accruing and make the approximation impossible to disprove. Season-long
            markets stay hidden from Recommended until the season is within three weeks.
          </p>

          <CfbMarketsTable rows={shown} />
        </>
      )}
    </PageShell>
  );
}
