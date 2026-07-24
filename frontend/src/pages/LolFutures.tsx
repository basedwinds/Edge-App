import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { FuturesTable } from "../components/markets/FuturesTable";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchLolFutures } from "../api/markets";
import { useFuturesMarkPlaced } from "../hooks/useFuturesMarkPlaced";

/** LoL's own dedicated Futures page -- see ValorantFutures.tsx's own
 * docstring for why every row here has no model estimate (no
 * tournament-bracket model built for any esports title yet). */
export function LolFutures() {
  const { data, isLoading, isError } = useQuery({ queryKey: ["lol", "futures"], queryFn: fetchLolFutures });
  const onMarkPlaced = useFuturesMarkPlaced("lol");
  const rows = data ?? [];

  const stats = useMemo(() => {
    const tournaments = new Set(rows.map((r) => r.group_label));
    return { tournamentCount: tournaments.size, rowCount: rows.length };
  }, [rows]);

  return (
    <PageShell title="LoL Futures">
      {isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}

      {isLoading ? (
        <>
          <StatTilesSkeleton />
          <TableSkeleton />
        </>
      ) : (
        <>
          <div className="flex flex-wrap border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] mb-6 divide-x divide-[var(--color-border)]">
            <StatTile label="Tournaments tracked" value={String(stats.tournamentCount)} sublabel="real Kalshi tournament-winner events" />
            <StatTile label="Markets tracked" value={String(stats.rowCount)} sublabel="one row per team in the field, per tournament" />
          </div>
          <FuturesTable rows={rows} onMarkPlaced={onMarkPlaced} sport="lol" />
        </>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        No tournament-bracket model exists for LoL yet (unlike NFL/NBA/MLB's season simulations or
        Tennis/Soccer's own bracket/round-robin Monte Carlo) -- these are real, live futures prices with
        no model estimate to compare against, so nothing here computes an edge or a suggested stake. This
        is an honest "real inventory, no model" listing, not a bug.
      </p>
    </PageShell>
  );
}
