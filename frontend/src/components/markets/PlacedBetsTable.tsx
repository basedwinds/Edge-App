import { useMemo, useState } from "react";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { ArrowUpDown, Trash2 } from "lucide-react";
import type { PlacedBetPayload } from "../../api/markets";
import { SourceBadge } from "./SourceBadge";
import { EdgeBadge } from "./EdgeBadge";

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

const STATUS_STYLE: Record<string, string> = {
  pending: "border-[var(--color-accent)]/40 text-[var(--color-accent)] bg-[var(--color-accent)]/10",
  won: "border-[var(--color-good)]/40 text-[var(--color-good)] bg-[var(--color-good)]/10",
  lost: "border-[var(--color-critical)]/40 text-[var(--color-critical)] bg-[var(--color-critical)]/10",
  push: "border-[var(--color-text-muted)]/40 text-[var(--color-text-muted)] bg-white/5",
  void: "border-[var(--color-text-muted)]/40 text-[var(--color-text-muted)] bg-white/5",
};

const columnHelper = createColumnHelper<PlacedBetPayload>();

export function PlacedBetsTable({
  rows,
  showSettleActions,
  onSettle,
  onDelete,
}: {
  rows: PlacedBetPayload[];
  showSettleActions: boolean;
  onSettle: (id: number, status: "won" | "lost" | "push" | "void") => void;
  onDelete: (id: number) => void;
}) {
  const [sorting, setSorting] = useState<SortingState>([{ id: "placed_at", desc: true }]);
  const data = useMemo(() => rows, [rows]);

  const columns = [
    columnHelper.accessor("label", {
      header: "Market",
      cell: (info) => <span className="font-medium whitespace-nowrap">{info.getValue()}</span>,
    }),
    columnHelper.display({
      id: "teamLine",
      header: "Pick",
      cell: ({ row }) => {
        const { team, line } = row.original;
        const parts = [team, line !== null ? String(line) : null].filter(Boolean);
        return <span className="font-medium whitespace-nowrap">{parts.length ? parts.join(" ") : "—"}</span>;
      },
    }),
    columnHelper.accessor("source", {
      header: "Source",
      cell: (info) => <SourceBadge source={info.getValue()} />,
    }),
    columnHelper.accessor("market_prob_at_placement", {
      header: "Market % (at placement)",
      cell: (info) => <span className="tabular-nums font-mono">{formatPct(info.getValue())}</span>,
    }),
    columnHelper.display({
      id: "decimalOdds",
      header: () => <span title="1 / price at placement -- for copying into an external tracker (BetDiary, etc.), since Kalshi/Polymarket quote in implied probability, not odds">Decimal Odds</span>,
      cell: ({ row }) => {
        const p = row.original.market_prob_at_placement;
        if (p === null || p <= 0) return <span className="text-[var(--color-text-muted)]">—</span>;
        return <span className="tabular-nums font-mono text-[var(--color-text-dim)]">{(1 / p).toFixed(3)}</span>;
      },
    }),
    columnHelper.accessor("edge_at_placement", {
      header: "Edge (at placement)",
      cell: (info) => <EdgeBadge edge={info.getValue()} />,
    }),
    columnHelper.accessor("clv_pp", {
      header: () => <span title="Closing Line Value: did the market move toward your side after you bet? Positive = you got a better price than the close. Futures/season-long bets show current-price-vs-placement instead, since there's no single closing moment for those.">CLV</span>,
      cell: (info) => {
        const { clv_status } = info.row.original;
        const v = info.getValue();
        if (clv_status === "pending") return <span className="text-xs text-[var(--color-text-muted)] italic">pending kickoff</span>;
        if (clv_status === "unavailable" || v === null) return <span className="text-xs text-[var(--color-text-muted)]">—</span>;
        const pp = v * 100;
        const label = clv_status === "not_applicable" ? "vs current" : "CLV";
        return (
          <span className={"text-xs tabular-nums font-mono font-medium " + (pp > 0 ? "text-[var(--color-good)]" : pp < 0 ? "text-[var(--color-critical)]" : "text-[var(--color-text-dim)]")}>
            {pp > 0 ? "+" : ""}{pp.toFixed(1)}pp <span className="text-[var(--color-text-muted)] font-normal">({label})</span>
          </span>
        );
      },
    }),
    columnHelper.display({
      id: "stake",
      header: "Stake",
      cell: ({ row }) => (
        <span className="tabular-nums font-mono font-medium">
          {row.original.stake_units !== null ? `${row.original.stake_units.toFixed(1)}u` : "—"}
          <span className="text-[var(--color-text-muted)] ml-1 font-normal">(${row.original.stake_dollars.toLocaleString()})</span>
        </span>
      ),
    }),
    columnHelper.accessor("stake_pool", {
      header: "Pool",
      cell: (info) => <span className="text-[var(--color-text-dim)] capitalize">{info.getValue()}</span>,
    }),
    columnHelper.accessor("placed_at", {
      header: "Placed",
      cell: (info) => <span className="text-[var(--color-text-dim)] font-mono whitespace-nowrap">{new Date(info.getValue()).toLocaleDateString()}</span>,
    }),
    columnHelper.accessor("status", {
      header: "Status",
      cell: (info) => (
        <span className={"inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium border capitalize " + (STATUS_STYLE[info.getValue()] ?? "")}>
          {info.getValue()}
        </span>
      ),
    }),
    columnHelper.display({
      id: "actions",
      header: "Actions",
      cell: ({ row }) => {
        const bet = row.original;
        if (bet.status === "pending") {
          return (
            <div className="flex items-center gap-1.5 flex-wrap">
              {showSettleActions && (
                <>
                  <button onClick={() => onSettle(bet.id, "won")} className="rounded-md border border-[var(--color-good)]/40 text-[var(--color-good)] px-2 py-1 text-xs hover:bg-[var(--color-good)]/10 transition-colors">Won</button>
                  <button onClick={() => onSettle(bet.id, "lost")} className="rounded-md border border-[var(--color-critical)]/40 text-[var(--color-critical)] px-2 py-1 text-xs hover:bg-[var(--color-critical)]/10 transition-colors">Lost</button>
                  <button onClick={() => onSettle(bet.id, "push")} className="rounded-md border border-[var(--color-border)] px-2 py-1 text-xs hover:bg-[var(--color-surface-hover)] transition-colors">Push</button>
                </>
              )}
              <button onClick={() => onDelete(bet.id)} title="Remove (mis-click)" className="rounded-md border border-[var(--color-border)] p-1 text-[var(--color-text-dim)] hover:text-[var(--color-critical)] hover:bg-[var(--color-surface-hover)] transition-colors">
                <Trash2 size={13} />
              </button>
            </div>
          );
        }
        return <span className="text-xs text-[var(--color-text-muted)]">{bet.settlement_note ?? "—"}</span>;
      },
    }),
  ];

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
      <table className="w-full text-sm border-collapse min-w-[900px]">
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
            <tr key={row.id} className="border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-surface-hover)] transition-colors">
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
                No bets here yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
