// Approximate "when does this settle?" estimates for the Bet Tracker, so each
// position shows roughly when it resolves (and, for futures, when the capital
// frees up). Game bets settle within hours of kickoff, so their date is exact.
// Futures resolve at a season/tournament end -- those are COARSE estimates
// anchored to the normal league calendar (NFL regular season ends early Jan,
// MLB late Sep, European soccer leagues in May), shown month-level ("~ Jan
// 2027") and always labelled an estimate. Never treated as a precise date.

export type Resolution = { label: string; sortKey: number };

const UNKNOWN: Resolution = { label: "—", sortKey: Number.MAX_SAFE_INTEGER };

function monthYear(iso: string): Resolution {
  const ms = Date.parse(iso);
  const d = new Date(ms);
  return { label: `~ ${d.toLocaleDateString(undefined, { month: "short", year: "numeric" })}`, sortKey: ms };
}

// Season-end anchors for the CURRENT cycle (recurring calendar facts, not the
// model's numbers). Day precision only matters for sort ordering; the UI shows
// month + year, so these read as the estimates they are.
const NFL_REG_END = "2027-01-05T00:00:00Z"; // end of the 18-week regular season
const NFL_CONF = "2027-01-24T00:00:00Z"; // conference championships
const NFL_SB = "2027-02-08T00:00:00Z"; // Super Bowl + end-of-season awards
const NFL_WEEK1 = "2026-09-10T00:00:00Z"; // week 1
const MLB_REG_END = "2026-09-28T00:00:00Z"; // end of the regular season
const MLB_POST = "2026-11-01T00:00:00Z"; // World Series
const SOCCER_END = "2027-05-24T00:00:00Z"; // final matchday of the European leagues
// MLS runs Feb->Dec, so its futures resolve FIVE MONTHS before the European
// leagues' do -- reusing SOCCER_END here would overstate the capital lockup on
// every MLS Cup position by about half a year. Taken from Kalshi's own
// expected_expiration_time on KXMLSCUP/KXMLSEAST/KXMLSWEST (all three agree),
// not estimated from the calendar.
const MLS_CUP = "2026-12-25T00:00:00Z";

// market_type -> resolution anchor, per sport. Covers the futures types the app
// actually prices; anything unmapped falls through to a generic estimate.
const FUTURES: Record<string, Record<string, string>> = {
  nfl: {
    division_winner: NFL_REG_END, division_wins: NFL_REG_END, division_order: NFL_REG_END,
    playoff_qualifier: NFL_REG_END, one_seed: NFL_REG_END, best_record: NFL_REG_END,
    h2h_wins: NFL_REG_END, win_total: NFL_REG_END, week1_qb: NFL_WEEK1,
    stage_of_elimination: NFL_SB, conference_champion: NFL_CONF, super_bowl: NFL_SB,
    opoy: NFL_SB, mvp: NFL_SB, dpoy: NFL_SB, oroy: NFL_SB, droy: NFL_SB, coy: NFL_SB,
  },
  mlb: {
    win_total: MLB_REG_END, playoff_qualifier: MLB_REG_END, division_winner: MLB_REG_END,
    pennant: MLB_POST, world_series: MLB_POST,
  },
  soccer: {
    league_winner: SOCCER_END, relegation: SOCCER_END, top4: SOCCER_END,
    top2: SOCCER_END, top6: SOCCER_END, top_half: SOCCER_END,
    mls_cup_winner: MLS_CUP, mls_conference_winner: MLS_CUP,
  },
};

/** Estimated resolution for a futures position (season/tournament end). */
export function futuresResolution(sport: string, marketType: string): Resolution {
  const iso = FUTURES[sport]?.[marketType];
  if (iso) return monthYear(iso);
  // Esports single-event futures resolve when that bracket finishes -- we don't
  // carry the event's end date on the placed bet, so it's a soft "tournament
  // end" (sorted just after this season's dated markets, before undated ones).
  if (marketType === "tournament_winner") return { label: "tournament end", sortKey: Date.parse("2026-12-31T00:00:00Z") };
  return UNKNOWN;
}

/** Game bets settle within hours of kickoff, so resolution ≈ the game date. */
export function gameResolution(startIso: string | null): Resolution {
  if (!startIso) return UNKNOWN;
  const ms = Date.parse(startIso);
  if (Number.isNaN(ms)) return UNKNOWN;
  const d = new Date(ms);
  return { label: d.toLocaleDateString(undefined, { month: "short", day: "numeric" }), sortKey: ms };
}
