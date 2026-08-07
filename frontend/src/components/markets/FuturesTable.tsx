import { useMemo, useState } from "react";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpDown, CheckCircle2, ChevronDown, ChevronUp, Hourglass, Info, TrendingUp } from "lucide-react";
import type { FuturesMarketRow } from "../../types/market";
import { fetchReadiness, isFuturesSportNotReady } from "../../api/markets";
import { SourceBadge } from "./SourceBadge";
import { EdgeBadge } from "./EdgeBadge";
import { FuturesTrendModal } from "./FuturesTrendModal";
import { BetReasoningModal } from "./BetReasoningModal";
import type { SportKey } from "../../lib/sports";
import { stakeableLegIds, collapseLadderRungs, MAX_STAKED_LEGS_PER_GROUP } from "../../utils/futuresGroupCap";


export const MARKET_TYPE_LABELS: Record<string, string> = {
  division_winner: "Division Winner",
  conference_champion: "Conference Champion",
  one_seed: "1 Seed",
  super_bowl_champion: "Super Bowl Champion",
  playoff_qualifier: "Playoff Qualifier",
  best_record: "Best Regular Season Record",
  undefeated_season: "Undefeated Season (any team)",
  win_total: "Season Win Total (O/U)",
  exact_win_total: "Exact Season Win Total",
  wins_any: "Any Team Hits Win Threshold",
  week1_qb: "Week 1 Starting QB",
  mvp: "MVP",
  coach_of_year: "Coach of the Year",
  opoy: "Offensive Player of the Year",
  dpoy: "Defensive Player of the Year",
  division_wins: "Division Total Wins",
  division_order: "Division Exact Order",
  div_least_wins: "Fewest-Wins Division",
  div_most_wins: "Most-Wins Division",
  worst_to_first: "Worst-to-First (any team)",
  h2h_wins: "Head-to-Head Win Total",
  leader_pass_yds: "Passing Yards Leader",
  leader_pass_tds: "Passing TDs Leader",
  leader_pass_int: "Interceptions Thrown Leader",
  leader_rush_yds: "Rushing Yards Leader",
  leader_rush_tds: "Rushing TDs Leader",
  leader_rec_yds: "Receiving Yards Leader",
  leader_rec_tds: "Receiving TDs Leader",
  leader_def_int: "Interceptions (Defense) Leader",
  leader_sacks: "Sacks Leader",
  team_pts_most: "Most Points Scored (Team)",
  team_pts_least: "Fewest Points Scored (Team)",
  team_dpts_most: "Most Points Allowed (Team)",
  team_dpts_least: "Fewest Points Allowed (Team)",
  season_pass_yds: "Season Passing Yards",
  season_rush_yds: "Season Rushing Yards",
  season_rush_tds: "Season Rushing TDs",
  season_rec_yds: "Season Receiving Yards",
  season_rec_tds: "Season Receiving TDs",
  season_rec: "Season Receptions",
  // NBA (2026-07-16) -- shares this same lookup/table component rather than
  // a duplicate NBA-only copy, since the shape is identical.
  championship: "Championship",
  play_in_qualifier: "Play-In Qualifier",
  worst_record: "Worst Regular Season Record",
  // Soccer (added 2026-07-19) -- shares this same table component too.
  league_winner: "League Winner",
  relegation: "Relegation",
  // EPL-only real inventory (added 2026-07-19, see kalshi_soccer_client.py::TOP_N_SERIES).
  top_half: "Top Half",
  top4: "Top 4",
  top2: "Top 2",
  // MLS Cup playoffs (added 2026-08-07). "Conference Winner" rather than
  // "Conference Champion" of the regular-season table: these resolve on the
  // BRACKET, and group_label already says which conference.
  mls_cup_winner: "MLS Cup Winner",
  mls_conference_winner: "Conference Winner",
  stage_of_elimination: "Stage of Elimination",
};

// stage_of_elimination: the distinguishing field is `side`, not line -- shown
// in the Line column via formatLine below.
const STAGE_OF_ELIM_LABELS: Record<string, string> = {
  reg: "Miss playoffs",
  wc: "Out: Wild Card",
  div: "Out: Divisional",
  conf: "Out: Conf. Champ.",
  sb_loss: "Lose Super Bowl",
  sb_win: "Win Super Bowl",
};

const SEASON_STAT_UNITS: Record<string, string> = {
  season_pass_yds: "yds",
  season_rush_yds: "yds",
  season_rec_yds: "yds",
  season_rush_tds: "TDs",
  season_rec_tds: "TDs",
  season_rec: "rec",
};

function formatLine(row: FuturesMarketRow): string {
  if (row.market_type === "stage_of_elimination") return STAGE_OF_ELIM_LABELS[row.side ?? ""] ?? row.side ?? "—";
  if (row.line === null || row.line === undefined) return "—";
  if (row.market_type === "exact_win_total") return `${row.line} wins`;
  if (row.market_type === "win_total" || row.market_type === "wins_any") return `${row.line}+ wins`;
  const unit = SEASON_STAT_UNITS[row.market_type];
  if (unit) return `${Math.ceil(row.line)}+ ${unit}`;
  return String(row.line);
}

const columnHelper = createColumnHelper<FuturesMarketRow>();

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

const columns = [
  columnHelper.accessor("market_type", {
    header: "Market",
    cell: (info) => (
      <span className="font-medium whitespace-nowrap">{MARKET_TYPE_LABELS[info.getValue()] ?? info.getValue()}</span>
    ),
  }),
  columnHelper.accessor("group_label", {
    header: "Group",
    cell: (info) => <span className="text-[var(--color-text-dim)] whitespace-nowrap">{info.getValue() ?? "—"}</span>,
  }),
  columnHelper.accessor("team", {
    header: "Team",
    cell: (info) => <span className="font-medium">{info.getValue() ?? "—"}</span>,
  }),
  columnHelper.display({
    id: "line",
    header: "Line",
    cell: ({ row }) => <span className="tabular-nums font-mono text-[var(--color-text-dim)] whitespace-nowrap">{formatLine(row.original)}</span>,
  }),
  columnHelper.accessor("source", {
    header: "Source",
    cell: (info) => <SourceBadge source={info.getValue()} />,
  }),
  columnHelper.accessor("implied_prob", {
    header: () => <span title="Team win probability implied by the market price">Market %</span>,
    cell: (info) => <span className="tabular-nums font-mono">{formatPct(info.getValue())}</span>,
  }),
  columnHelper.accessor("model_prob", {
    header: () => (
      <span title="Season Monte Carlo simulation using current Elo ratings -- not validated to beat the market, see disclaimer">
        Est. %
      </span>
    ),
    cell: (info) => {
      const note = info.row.original.model_note;
      return (
        <span className="tabular-nums font-mono text-[var(--color-text-dim)] inline-flex items-center gap-1">
          {formatPct(info.getValue())}
          {note && (
            <span
              title={note}
              className="cursor-help rounded-sm border border-[var(--color-warning)]/40 bg-[var(--color-warning)]/10 text-[var(--color-warning)] text-[9px] px-1 leading-tight"
            >
              approx
            </span>
          )}
        </span>
      );
    },
  }),
  columnHelper.accessor("edge", {
    header: "Edge",
    cell: (info) => <EdgeBadge edge={info.getValue()} />,
  }),
  columnHelper.accessor("volume", {
    header: "Volume",
    cell: (info) => (
      <span className="tabular-nums font-mono text-[var(--color-text-dim)]">
        {info.getValue() ? Math.round(info.getValue()!).toLocaleString() : "—"}
      </span>
    ),
  }),
  columnHelper.accessor("suggested_stake_dollars", {
    header: () => <span title="Quarter Kelly, capped at 5% of its pool (weekly or futures) -- see Settings">Stake</span>,
    cell: (info) => {
      const kelly = info.row.original.kelly_fraction;
      const units = info.row.original.suggested_stake_units;
      if (info.getValue() === null || kelly === null) {
        return <span className="text-[var(--color-text-muted)]">—</span>;
      }
      return (
        <span className="tabular-nums font-mono text-[var(--color-good)]">
          {units !== null
            // 1dp renders a 0.25u futures stake as "0.3u", which reads as a
            // number nobody set. Sub-unit stakes get 2dp so the figure on
            // screen is the one the pool cap actually produced.
            ? `${units < 1 ? units.toFixed(2) : units.toFixed(1)}u`
            : `$${info.getValue()!.toLocaleString()}`}
          <span className="text-[var(--color-text-muted)] ml-1">(${info.getValue()!.toLocaleString()}, {(kelly * 100).toFixed(1)}%)</span>
        </span>
      );
    },
  }),
];

export function FuturesTable({ rows, onMarkPlaced, sport }: { rows: FuturesMarketRow[]; onMarkPlaced?: (row: FuturesMarketRow) => void; sport?: SportKey }) {
  // Sorted by EDGE, best first. This used to lead on market_type then
  // implied_prob, i.e. "most likely to happen" -- which put the market's own
  // favourites on top and buried the rows the model actually disagrees with.
  // Futures capacity is rationed by hand (you mark bets placed), so whatever is
  // at the top of this table is what gets funded; leading on implied_prob meant
  // the ordering worked against the point of the page.
  const [sorting, setSorting] = useState<SortingState>([
    { id: "edge", desc: true },
  ]);
  const [reasoningRow, setReasoningRow] = useState<FuturesMarketRow | null>(null);
  const [trendRow, setTrendRow] = useState<FuturesMarketRow | null>(null);
  // A settled group is a RESULT, not an opportunity. It used to be dropped by
  // the backend, so a decided future vanished from the page rather than
  // reading as finished (the reported case: a champion market disappearing).
  // It is returned flagged now and filed below, collapsed, so the live list
  // stays short instead of being padded with dead legs.
  // Cap real exposure per futures group: past the cap a leg still renders, it
  // just carries no suggested stake. See futuresGroupCap -- the hazard is
  // correlation (16 staked legs of one win-total ladder is one opinion sixteen
  // times), not mutual exclusivity, which measured out fine.
  const capped = useMemo(() => {
    // Two passes: collapse each team's ladder to its best rung FIRST (COL 35+/
    // 40+/45+ is one nested opinion, not three), then cap legs per group.
    const unladdered = collapseLadderRungs(rows);
    const allowed = stakeableLegIds(rows.filter((r) => unladdered.has(r.id)));
    return rows.map((r) =>
      r.suggested_stake_dollars != null && !allowed.has(r.id)
        ? { ...r, suggested_stake_dollars: null, suggested_stake_units: null, kelly_fraction: null }
        : r,
    );
  }, [rows]);
  const cappedOut = useMemo(
    () => rows.filter((r) => r.suggested_stake_dollars != null).length
      - capped.filter((r) => r.suggested_stake_dollars != null).length,
    [rows, capped],
  );
  const activeRows = useMemo(() => capped.filter((r) => !r.group_settled), [capped]);
  const settledRows = useMemo(() => capped.filter((r) => r.group_settled), [capped]);
  const [showSettled, setShowSettled] = useState(false);
  // Collapsed to ONE LINE PER EVENT: 32 dead legs of a finished tournament is
  // scrolling, not information. The winner is the only part still worth reading.
  const settledGroups = useMemo(() => {
    const by = new Map<string, { label: string; winner: string | null; legs: number }>();
    for (const r of settledRows) {
      const label = r.group_label ?? "(ungrouped)";
      const cur = by.get(label) ?? { label, winner: r.group_winner ?? null, legs: 0 };
      cur.legs += 1;
      if (!cur.winner && r.group_winner) cur.winner = r.group_winner;
      by.set(label, cur);
    }
    return [...by.values()].sort((a, b) => a.label.localeCompare(b.label));
  }, [settledRows]);
  const data = activeRows;
  const readiness = useQuery({ queryKey: ["readiness"], queryFn: fetchReadiness }).data;

  // Actions column: a "why" (reasoning) button when a sport is provided, and a
  // "Mark placed" button when the page wires a handler. Built here (not at
  // module scope) so the buttons close over sport/onMarkPlaced.
  const allColumns = useMemo(() => {
    return [
      ...columns,
      columnHelper.display({
        id: "actions",
        header: "",
        cell: ({ row }) => (
          <div className="flex items-center justify-end gap-1.5">
            <button
              onClick={() => setTrendRow(row.original)}
              className="p-1 rounded-md border border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)]"
              title="How the price and the model have moved over time"
            >
              <TrendingUp size={13} />
            </button>
            {sport && (
              <button
                onClick={() => setReasoningRow(row.original)}
                className="p-1 rounded-md border border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)]"
                title="Why this number? (model explanation)"
              >
                <Info size={13} />
              </button>
            )}
            {onMarkPlaced && (
              <button
                onClick={() => onMarkPlaced(row.original)}
                className="text-xs font-medium px-2 py-1 rounded-md border border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)] whitespace-nowrap"
                title="Log this futures position in the tracker (Futures section)"
              >
                Mark placed
              </button>
            )}
          </div>
        ),
      }),
    ];
  }, [onMarkPlaced, sport]);

  const table = useReactTable({
    data,
    columns: allColumns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  // Season sports hide their futures until the season is active/near (same rule
  // as the Discord alerts + Recommended list): show a "not ready yet" notice
  // instead of prematurely-priced rows (rosters aren't set, so the price is noise).
  if (isFuturesSportNotReady(sport, readiness)) {
    return (
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-10 text-center text-sm text-[var(--color-text-dim)] flex flex-col items-center gap-2">
        <Hourglass size={20} className="text-[var(--color-text-muted)]" />
        <div>Futures aren't ready yet — they open about 3 weeks before the season starts, once rosters settle.</div>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-x-auto">
      <table className="w-full text-sm border-collapse min-w-[720px]">
        <thead>
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id} className="border-b border-[var(--color-border)]">
              {hg.headers.map((header) => (
                <th
                  key={header.id}
                  onClick={header.column.getToggleSortingHandler()}
                  className="text-left px-4 py-3 text-xs uppercase tracking-wide text-[var(--color-text-dim)] font-medium cursor-pointer select-none hover:text-[var(--color-text)] transition-colors whitespace-nowrap"
                >
                  <span className="inline-flex items-center gap-1">
                    {flexRender(header.column.columnDef.header, header.getContext())}
                    {header.column.getIsSorted() && <ArrowUpDown size={12} />}
                  </span>
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr
              key={row.id}
              className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-surface-hover)] transition-colors"
            >
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id} className="px-4 py-3 whitespace-nowrap">
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
          {table.getRowModel().rows.length === 0 && (
            <tr>
              <td colSpan={columns.length} className="px-4 py-10 text-center text-[var(--color-text-dim)]">
                {settledRows.length > 0
                  ? "No live futures here — every tracked group has been decided."
                  : "No futures markets tracked yet."}
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {cappedOut > 0 && (
        <p className="px-4 pb-3 text-[11px] text-[var(--color-text-muted)]">
          {cappedOut} more {cappedOut === 1 ? "leg qualifies" : "legs qualify"} but {cappedOut === 1 ? "is" : "are"} shown
          without a stake — at most {MAX_STAKED_LEGS_PER_GROUP} legs of one group get sized, so a single
          ladder can't turn one model opinion into a dozen correlated positions.
        </p>
      )}
      {settledRows.length > 0 && (
        <div className="border-t border-[var(--color-border)]">
          <button
            onClick={() => setShowSettled((v) => !v)}
            className="w-full flex items-center justify-between px-4 py-3 text-sm text-[var(--color-text-dim)] hover:text-[var(--color-text)] transition-colors"
          >
            <span className="inline-flex items-center gap-2">
              <CheckCircle2 size={14} className="text-[var(--color-text-muted)]" />
              Settled ({settledGroups.length} {settledGroups.length === 1 ? "event" : "events"},{" "}
              {settledRows.length} legs)
            </span>
            {showSettled ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          </button>
          {showSettled && (
            <div className="px-4 pb-4 flex flex-col gap-2">
              {settledGroups.map((g) => (
                <div
                  key={g.label}
                  className="flex items-center justify-between gap-3 rounded-md border border-[var(--color-border)] px-3 py-2"
                >
                  <span className="text-sm text-[var(--color-text)] truncate">{g.label}</span>
                  <span className="text-xs text-[var(--color-text-dim)] whitespace-nowrap">
                    {g.winner ? <>Won by <span className="text-[var(--color-text)]">{g.winner}</span></> : "Decided"}
                    <span className="text-[var(--color-text-muted)]"> · {g.legs} legs</span>
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {trendRow && (
        <FuturesTrendModal
          marketId={trendRow.id}
          title={`${trendRow.team ?? "This leg"} — ${trendRow.group_label ?? ""}`}
          onClose={() => setTrendRow(null)}
        />
      )}
      {reasoningRow && sport && (
        <BetReasoningModal
          marketId={reasoningRow.id}
          modelProb={reasoningRow.model_prob}
          marketProb={reasoningRow.implied_prob}
          sport={sport}
          onClose={() => setReasoningRow(null)}
        />
      )}
    </div>
  );
}
