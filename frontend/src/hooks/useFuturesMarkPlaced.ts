import { useQueryClient } from "@tanstack/react-query";
import { markFuturesBetPlaced } from "../api/markets";
import type { FuturesMarketRow } from "../types/market";

/** Shared "Mark placed" handler for every sport's Futures page.
 *
 * Places ONLY what the model actually sized, and invalidates the tracker +
 * placed-bet queries so the new position shows up immediately in the tracker's
 * Futures section. */
export function useFuturesMarkPlaced(sport: string) {
  const queryClient = useQueryClient();
  return async (row: FuturesMarketRow) => {
    // The model declining to size a future is a REFUSAL, not a gap to fill.
    // This fell back to a fabricated stake (originally a full unit, later
    // 0.25u), so the legs the safety gates had just rejected were the ones
    // offered a number -- see CrossSportFuturesTable.place for the real case.
    if (row.suggested_stake_dollars == null) return;
    const stakeDollars = row.suggested_stake_dollars;
    const stakeUnits = row.suggested_stake_units ?? 0;
    await markFuturesBetPlaced(row, sport, stakeDollars, stakeUnits);
    queryClient.invalidateQueries({ queryKey: ["portfolio"] });
    queryClient.invalidateQueries({ queryKey: ["open-bets"] });
    queryClient.invalidateQueries({ queryKey: ["settled-bets"] });
    queryClient.invalidateQueries({ queryKey: ["placed-bets", sport] });
  };
}
