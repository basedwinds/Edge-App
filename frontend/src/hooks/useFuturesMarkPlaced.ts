import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FALLBACK_FUTURES_UNITS } from "../utils/futuresGroupCap";
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
    // A future the model DECLINED to size (approximate bracket, no volume,
    // tracking-only) used to fall back to a FULL unit -- four times what the
    // sized ones get, which is exactly backwards: the least-trustworthy legs
    // were booked biggest. Falls back to the same 0.25u the pool cap gives
    // everything else.
    const stakeDollars = row.suggested_stake_dollars
      ?? (unitDollars > 0 ? unitDollars * FALLBACK_FUTURES_UNITS : 2.5);
    const stakeUnits = row.suggested_stake_units ?? FALLBACK_FUTURES_UNITS;
    await markFuturesBetPlaced(row, sport, stakeDollars, stakeUnits);
    queryClient.invalidateQueries({ queryKey: ["portfolio"] });
    queryClient.invalidateQueries({ queryKey: ["open-bets"] });
    queryClient.invalidateQueries({ queryKey: ["settled-bets"] });
    queryClient.invalidateQueries({ queryKey: ["placed-bets", sport] });
  };
}
