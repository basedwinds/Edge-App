/** Tennis spread wording, in one place because the two spread markets use
 * OPPOSITE sign conventions and nothing on screen said so.
 *
 * `game_spread` follows this app's own convention, the one
 * game_lines_tennis.py::prob_game_spread_cover documents and every other
 * sport's spread shares: the line is the margin the picked player must
 * EXCEED, so a favourite's line is POSITIVE and an underdog's is negative.
 *
 * `set_spread` is the other way round -- ordinary bookmaker notation.
 * tennis_markets.py::_set_spread_model_prob prices `line < 0` as
 * `p_win * blowout`, i.e. **-1.5 means "wins by 2+ sets"**, and it says so
 * after testing 120 resolved markets (the set reading was right 120/120,
 * the games reading 73/120).
 *
 * So the same "-1.5" means opposite things on two markets of the same match.
 * Rendering either as a raw signed number is unreadable, and rendering them
 * with the SAME rule would be wrong on one of them -- a real hazard, since
 * "Kostyuk -1.5 sets" flipped is the difference between needing a 2-0 sweep
 * and merely needing to avoid one. Both get spelled out in words instead.
 */
export function describeTennisSpread(
  marketType: string,
  team: string | null,
  line: number | null,
): string | null {
  if (line === null || !team) return null;
  const by = Math.ceil(Math.abs(line));
  if (marketType === "set_spread") {
    if (line < 0) return `${team} wins by ${by}+ sets`;
    if (line > 0) return `${team} doesn't lose by ${by}+ sets`;
    return `${team} wins outright`;
  }
  if (marketType === "game_spread") {
    if (line > 0) return `${team} wins by ${by}+ games`;
    if (line < 0) return `${team} doesn't lose by ${by}+ games`;
    return `${team} wins outright`;
  }
  return null;
}

/** Market-type names for the tennis-only types. Kept beside the wording above
 * so a new type can't get one without the other. */
export const TENNIS_MARKET_TYPE_LABELS: Record<string, string> = {
  set_winner: "Set Winner",
  set_spread: "Set Handicap",
  set_total: "Set Total",
  total_sets: "Total Sets",
  game_spread: "Game Spread",
  game_total: "Game Total",
  exact_score: "Exact Score",
};
