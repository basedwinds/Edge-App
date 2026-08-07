// Shared "describe a futures position clearly" helpers, used by both the All
// Bets Futures table and the Bet Tracker's Futures list -- so a row reads
// "WSH · Season Win Total · 70+ wins" instead of a bare "WSH", and you can
// always tell exactly what the position is.

export const STAGE_OF_ELIM_LABELS: Record<string, string> = {
  reg: "Miss playoffs",
  wc: "Out: Wild Card",
  div: "Out: Divisional",
  conf: "Out: Conf. Champ.",
  sb_loss: "Lose Super Bowl",
  sb_win: "Win Super Bowl",
};

// Fallback readable name per market_type, used when the row has no group_label
// (Kalshi supplies a descriptive group_label for most futures, but not all —
// e.g. win-total ladders come through with a null label).
const MARKET_NAME: Record<string, string> = {
  win_total: "Season Win Total",
  exact_win_total: "Exact Win Total",
  wins_any: "Win Total",
  division_winner: "Division Winner",
  division_order: "Division: Exact Order",
  division_wins: "Division Total Wins",
  conference_champion: "Conference Champion",
  super_bowl_champion: "Champion",
  championship: "Champion",
  league_winner: "League Champion",
  playoff_qualifier: "Make Playoffs",
  play_in_qualifier: "Make Play-In",
  one_seed: "#1 Seed",
  best_record: "Best Record",
  worst_record: "Worst Record",
  stage_of_elimination: "Stage of Elimination",
  h2h_wins: "Head-to-Head Wins",
  relegation: "Relegation",
  mls_cup_winner: "MLS Cup Winner",
  mls_conference_winner: "Conference Winner",
  top2: "Top 2",
  top4: "Top 4",
  top6: "Top 6",
  top_half: "Top Half",
  tournament_winner: "Tournament Winner",
  pennant: "Pennant",
  world_series: "World Series",
  race_winner: "Race Winner",
  top_n: "Top-N Finish",
  pole: "Pole",
};

type FutLike = { market_type: string; side: string | null; line: number | null; group_label?: string | null };

// A placed bet's stored label is sometimes the generic "MLB win_total" /
// "NFL week1_qb" (uppercase code + lowercase_type) rather than a real market
// name -- treat those as absent so the readable fallback is used instead.
const GENERIC_LABEL = /^[A-Za-z]{2,5} [a-z][a-z_]*$/;

/** The specific market name: the source's own group_label when present and
 * descriptive (e.g. "AFC East Division Winner", "US Open (Men's)"), else a
 * readable fallback keyed on the market type. */
export function futuresMarketName(p: FutLike): string {
  if (p.group_label && !GENERIC_LABEL.test(p.group_label)) return p.group_label;
  return MARKET_NAME[p.market_type] || p.group_label || p.market_type;
}

/** The threshold/qualifier of the pick, e.g. "70+ wins", "Miss playoffs",
 * "Over 2.5" — empty when the pick (team) already says everything. */
export function futuresThreshold(p: FutLike): string {
  if (p.market_type === "stage_of_elimination") return STAGE_OF_ELIM_LABELS[p.side ?? ""] ?? p.side ?? "";
  if (p.line == null) return "";
  if (p.market_type === "exact_win_total") return `${p.line} wins`;
  if (p.market_type === "win_total" || p.market_type === "wins_any") return `${p.line}+ wins`;
  const dir = p.side === "over" ? "Over " : p.side === "under" ? "Under " : "";
  return `${dir}${p.line}`;
}
