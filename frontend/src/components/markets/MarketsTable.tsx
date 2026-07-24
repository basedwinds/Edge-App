import { useMemo, useState } from "react";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { ArrowUpDown } from "lucide-react";
import { useNavigate } from "react-router-dom";
import type { GameMarketRow } from "../../types/market";
import { SourceBadge } from "./SourceBadge";
import { EdgeBadge } from "./EdgeBadge";

const columnHelper = createColumnHelper<GameMarketRow>();

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function formatDate(iso: string) {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const columns = [
  columnHelper.accessor("gameday", {
    header: "Date",
    cell: (info) => (
      <span className="text-[var(--color-text-dim)] whitespace-nowrap">{formatDate(info.getValue())}</span>
    ),
  }),
  columnHelper.accessor("game_label", {
    header: "Game",
    cell: (info) => <span className="font-medium whitespace-nowrap">{info.getValue()}</span>,
  }),
  columnHelper.accessor("source", {
    header: "Source",
    cell: (info) => <SourceBadge source={info.getValue()} />,
  }),
  columnHelper.display({
    id: "predictedScore",
    header: () => <span title="Combines the spread model's expected margin with the totals model's expected combined score -- not validated, see disclaimer">Predicted Score</span>,
    cell: ({ row }) => {
      const { predictedHomeScore, predictedAwayScore, game_label, homeTeam } = row.original;
      if (predictedHomeScore === null || predictedAwayScore === null) {
        return <span className="text-[var(--color-text-muted)]">—</span>;
      }
      const awayTeam = game_label.split(" @ ")[0];
      return (
        <span className="tabular-nums text-[var(--color-text-dim)] whitespace-nowrap">
          {homeTeam} {predictedHomeScore.toFixed(0)} - {awayTeam} {predictedAwayScore.toFixed(0)}
        </span>
      );
    },
  }),
  columnHelper.accessor("homeImpliedProb", {
    header: () => <span title="Home team win probability implied by the market price">Market %</span>,
    cell: (info) => <span className="tabular-nums font-mono">{formatPct(info.getValue())}</span>,
  }),
  columnHelper.accessor("homeBestProb", {
    header: () => (
      <span title="Elo baseline, blended with a news/research adjustment when one is on file -- not validated to beat the market, see disclaimer">
        Est. %
      </span>
    ),
    cell: (info) => {
      const reason = info.row.original.noBaselineReason;
      if (reason) {
        return (
          <span title={reason} className="text-[var(--color-text-muted)] italic text-xs">
            No baseline (preseason)
          </span>
        );
      }
      return (
        <span className="tabular-nums font-mono text-[var(--color-text-dim)] inline-flex items-center gap-1">
          {formatPct(info.getValue())}
          {info.row.original.hasNews && (
            <span title="Includes a news/research adjustment" className="text-[var(--color-good)]">
              •
            </span>
          )}
        </span>
      );
    },
  }),
  columnHelper.accessor("edge", {
    header: "Edge",
    cell: (info) =>
      info.row.original.noBaselineReason ? (
        <span className="text-[var(--color-text-muted)]">—</span>
      ) : (
        <EdgeBadge edge={info.getValue()} />
      ),
  }),
  columnHelper.accessor("volume", {
    header: "Volume",
    cell: (info) => (
      <span className="tabular-nums font-mono text-[var(--color-text-dim)]">
        {info.getValue() ? Math.round(info.getValue()!).toLocaleString() : "—"}
      </span>
    ),
  }),
  columnHelper.accessor("suggestedStakeDollars", {
    header: () => <span title="Quarter Kelly, capped at 5% of its pool (weekly or futures) -- see Settings">Stake</span>,
    cell: (info) => {
      const kelly = info.row.original.kellyFraction;
      const units = info.row.original.suggestedStakeUnits;
      if (info.getValue() === null || kelly === null) {
        return <span className="text-[var(--color-text-muted)]">—</span>;
      }
      return (
        <span className="tabular-nums font-mono text-[var(--color-good)]">
          {units !== null ? `${units.toFixed(1)}u` : `$${info.getValue()!.toLocaleString()}`}
          <span className="text-[var(--color-text-muted)] ml-1">(${info.getValue()!.toLocaleString()}, {(kelly * 100).toFixed(1)}%)</span>
        </span>
      );
    },
  }),
];

export function MarketsTable({ rows, defaultSort }: { rows: GameMarketRow[]; defaultSort?: SortingState }) {
  const [sorting, setSorting] = useState<SortingState>(defaultSort ?? [{ id: "gameday", desc: false }]);
  const navigate = useNavigate();
  const data = useMemo(() => rows, [rows]);

  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

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
              onClick={() => navigate(`/markets/${encodeURIComponent(row.original.key)}`)}
              className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-surface-hover)] cursor-pointer transition-colors"
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
                No matched games with live markets yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
