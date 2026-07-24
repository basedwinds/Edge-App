import type { MarketRow } from "../../types/market";
import { EdgeBadge } from "./EdgeBadge";

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function formatLine(line: number | null) {
  if (line === null) return "—";
  return line > 0 ? `+${line}` : `${line}`;
}

function LadderSection({
  title,
  lineCount,
  headerLabel,
  rowLabel,
  rows,
}: {
  title: string;
  lineCount: number;
  headerLabel: string;
  rowLabel: (r: MarketRow) => string;
  rows: MarketRow[];
}) {
  if (rows.length === 0) return null;
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-x-auto">
      <div className="px-4 py-3 border-b border-[var(--color-border)] text-sm font-medium">
        {title} ({lineCount} line{lineCount === 1 ? "" : "s"})
      </div>
      <table className="w-full text-sm border-collapse min-w-[480px]">
        <thead>
          <tr className="border-b border-[var(--color-border)] text-xs uppercase tracking-wide text-[var(--color-text-dim)]">
            <th className="text-left px-4 py-2 font-medium">{headerLabel}</th>
            <th className="text-left px-4 py-2 font-medium">Market %</th>
            <th className="text-left px-4 py-2 font-medium">Est. %</th>
            <th className="text-left px-4 py-2 font-medium">Edge</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-[var(--color-border)] last:border-0">
              <td className="px-4 py-2 whitespace-nowrap font-medium capitalize">{rowLabel(r)}</td>
              <td className="px-4 py-2 tabular-nums">{formatPct(r.implied_prob)}</td>
              <td className="px-4 py-2 tabular-nums text-[var(--color-text-dim)]">
                {r.no_baseline_reason ? (
                  <span className="italic text-xs text-[var(--color-text-muted)]">No baseline (preseason)</span>
                ) : (
                  formatPct(r.model_prob)
                )}
              </td>
              <td className="px-4 py-2">
                <EdgeBadge edge={r.no_baseline_reason ? null : r.edge} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function sortSpreadLike(rows: MarketRow[], homeTeam: string): MarketRow[] {
  return [...rows].sort(
    (a, b) => (a.team === b.team ? 0 : a.team === homeTeam ? -1 : 1) || (a.line ?? 0) - (b.line ?? 0)
  );
}

function sortTotalLike(rows: MarketRow[], homeTeam: string): MarketRow[] {
  return [...rows].sort(
    (a, b) =>
      (a.team === b.team ? 0 : a.team === homeTeam ? -1 : 1) ||
      (a.line ?? 0) - (b.line ?? 0) ||
      (a.side ?? "").localeCompare(b.side ?? "")
  );
}

/** Both platforms list several thresholds per game (a "ladder"), not one
 * traditional single line -- see market_matcher.py/kalshi_client.py for why.
 * Spread-like rows are shown one row per (team, line); total-like rows are
 * paired over/under on the same line. Half-line markets (Kalshi only, not
 * yet open for NFL as of this build) reuse the exact same section shape. */
export function SpreadTotalLadder({
  rows,
  homeTeam,
  awayTeam,
}: {
  rows: MarketRow[];
  homeTeam: string;
  awayTeam: string;
}) {
  const byType = (type: string) => rows.filter((r) => r.market_type === type);

  const spread = sortSpreadLike(byType("spread"), homeTeam);
  const total = sortTotalLike(byType("total"), homeTeam);
  const teamTotal = sortTotalLike(byType("team_total"), homeTeam);
  const spread1h = sortSpreadLike(byType("spread_1h"), homeTeam);
  const spread2h = sortSpreadLike(byType("spread_2h"), homeTeam);
  const total1h = sortTotalLike(byType("total_1h"), homeTeam);
  const total2h = sortTotalLike(byType("total_2h"), homeTeam);

  const anyRows = [spread, total, teamTotal, spread1h, spread2h, total1h, total2h].some((r) => r.length > 0);
  if (!anyRows) return null;

  return (
    <div className="mt-4 space-y-4">
      <LadderSection
        title="Spread"
        lineCount={spread.length}
        headerLabel="Team wins by more than"
        rowLabel={(r) => `${r.team} ${formatLine(r.line)}`}
        rows={spread}
      />
      <LadderSection
        title="Total"
        lineCount={total.length / 2}
        headerLabel="Line"
        rowLabel={(r) => `${r.side} ${r.line}`}
        rows={total}
      />
      <LadderSection
        title="Team Total"
        lineCount={teamTotal.length / 2}
        headerLabel="Team & line"
        rowLabel={(r) => `${r.team} ${r.side} ${r.line}`}
        rows={teamTotal}
      />
      <LadderSection
        title="1st Half Spread"
        lineCount={spread1h.length}
        headerLabel="Team wins 1st half by more than"
        rowLabel={(r) => `${r.team} ${formatLine(r.line)}`}
        rows={spread1h}
      />
      <LadderSection
        title="2nd Half Spread"
        lineCount={spread2h.length}
        headerLabel="Team wins 2nd half by more than"
        rowLabel={(r) => `${r.team} ${formatLine(r.line)}`}
        rows={spread2h}
      />
      <LadderSection
        title="1st Half Total"
        lineCount={total1h.length / 2}
        headerLabel="Line"
        rowLabel={(r) => `${r.side} ${r.line}`}
        rows={total1h}
      />
      <LadderSection
        title="2nd Half Total"
        lineCount={total2h.length / 2}
        headerLabel="Line"
        rowLabel={(r) => `${r.side} ${r.line}`}
        rows={total2h}
      />

      <p className="text-xs text-[var(--color-text-muted)]">
        {awayTeam} @ {homeTeam} · both platforms list several threshold lines per game (a "ladder"),
        not one traditional single line -- each row above is priced independently.
      </p>
    </div>
  );
}
