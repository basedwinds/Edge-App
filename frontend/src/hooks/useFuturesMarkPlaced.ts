import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchSettings, markFuturesBetPlaced } from "../api/markets";
import type { FuturesMarketRow } from "../types/market";

/** Shared "Mark placed" handler for every sport's Futures page. Falls back to a
 * 1-unit stake when the model didn't size one (many futures are tracking-only /
 * untraded), and invalidates the tracker + placed-bet queries so the new
 * position shows up immediately in the tracker's Futures section. */
export function useFuturesMarkPlaced(sport: string) {
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  return async (row: FuturesMarketRow) => {
    const unitDollars = settingsQuery.data?.unit_dollars ?? 0;
    const stakeDollars = row.suggested_stake_dollars ?? (unitDollars > 0 ? unitDollars : 10);
    const stakeUnits = row.suggested_stake_units ?? (unitDollars > 0 ? 1 : null);
    await markFuturesBetPlaced(row, sport, stakeDollars, stakeUnits);
    queryClient.invalidateQueries({ queryKey: ["portfolio"] });
    queryClient.invalidateQueries({ queryKey: ["open-bets"] });
    queryClient.invalidateQueries({ queryKey: ["settled-bets"] });
    queryClient.invalidateQueries({ queryKey: ["placed-bets", sport] });
  };
}
