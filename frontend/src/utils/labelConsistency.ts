/**
 * Does the LABEL describe the same side the model priced?
 *
 * WHY THIS EXISTS. On 2026-08-06 a recommended CS2 bet read "BORRACHEIROS wins
 * by 2+ maps" while the model had priced BORRACHEIROS +1.5 at 0.7175 -- which
 * is "doesn't get swept", the opposite proposition (the 2-0 side was worth
 * 0.1754). The number and the stake were correct; the words around them named
 * the other side. pickLabel had assumed series_handicap shares the SPREAD sign
 * convention, and it does not.
 *
 * The backend integrity checks cannot catch this, because the DATA was right.
 * Nothing catches a label that lies about correct data except reading it, which
 * is how all three display defects that session were found -- by a human.
 *
 * WHAT THIS CHECKS, AND WHAT IT DELIBERATELY DOES NOT.
 *
 * The first version of this file compared the label against the model
 * probabilities on live rows: "the side described as harder must carry the
 * lower probability". That is WRONG and was caught before shipping by running
 * it -- it flagged 3 rows, all of them correct. Paper Rex (1843) vs VARREL
 * (1568) has map_p 0.82, so P(2-0) = 0.672: for a dominant enough favourite the
 * sweep genuinely IS the likeliest outcome, and the harder label legitimately
 * carries the higher probability. Two opposing rows cannot distinguish an
 * inverted label from a dominant favourite, because both look identical.
 *
 * So this pins the CONVENTION instead. That does restate a rule the model also
 * encodes, which is normally worth avoiding -- but a golden assertion is
 * exactly the tool for "this must not silently flip", and a silent flip is the
 * bug. The conventions below are quoted from the functions that define them, so
 * a reader can check the claim rather than trust it.
 */
import { describePick } from "./pickLabel";

export type ConventionViolation = {
  marketType: string;
  sport: string;
  line: number;
  label: string;
  expected: string;
};

/**
 * The signed-margin conventions, each quoted from its defining function.
 *
 *   series_handicap  elo_cs2.py / elo_valorant.py
 *                      prob_handicap_cover_a(line) = P(a - b > -line)
 *                    The sign is FLIPPED: a NEGATIVE line is the team giving
 *                    maps (must win by that margin), POSITIVE is receiving.
 *
 *   spread / game_spread / spread_1h / spread_2h
 *                    game_lines.py prob_team_covers(line) = P(margin > line)
 *                    NOT flipped: a POSITIVE line is "wins by more than line".
 *
 * Tennis's set_spread/game_spread are deliberately absent: pickLabel's own
 * comment records that its two spreads disagree with each other on what a
 * negative line means, so asserting one rule over both would encode a claim
 * that is not true. They need their own measured convention first.
 */
const EXPECTATIONS: Array<{
  marketType: string;
  sport: string;
  /** Sign of `line` that should read as "team must win by that margin". */
  givingSign: -1 | 1;
}> = [
  { marketType: "series_handicap", sport: "cs2", givingSign: -1 },
  { marketType: "series_handicap", sport: "valorant", givingSign: -1 },
  { marketType: "spread", sport: "nfl", givingSign: 1 },
  { marketType: "spread", sport: "nba", givingSign: 1 },
  { marketType: "spread", sport: "mlb", givingSign: 1 },
  { marketType: "game_spread", sport: "soccer", givingSign: 1 },
];

const MAGNITUDE = 1.5;

function saysWinsBy(label: string): boolean {
  const l = label.toLowerCase();
  if (l.includes("doesn't lose") || l.includes("does not lose")) return false;
  return l.includes("wins by");
}

/**
 * Returns the conventions that pickLabel currently gets wrong. Empty is the
 * healthy state. Takes no data -- it interrogates the label function directly.
 */
export function findConventionViolations(): ConventionViolation[] {
  const out: ConventionViolation[] = [];
  for (const { marketType, sport, givingSign } of EXPECTATIONS) {
    const givingLine = MAGNITUDE * givingSign;
    const receivingLine = -givingLine;

    const givingLabel = describePick({
      team: "TEAM", line: givingLine, side: null, marketType, sport,
    });
    const receivingLabel = describePick({
      team: "TEAM", line: receivingLine, side: null, marketType, sport,
    });

    if (!saysWinsBy(givingLabel)) {
      out.push({
        marketType, sport, line: givingLine, label: givingLabel,
        expected: `a ${givingLine > 0 ? "positive" : "negative"} line is the giving side — should read "wins by"`,
      });
    }
    // The receiving side must NOT also read as "wins by". Some market types
    // render it neutrally ("TEAM +1.5"), which is fine -- only claiming the
    // giving side's meaning is a defect.
    if (saysWinsBy(receivingLabel)) {
      out.push({
        marketType, sport, line: receivingLine, label: receivingLabel,
        expected: `a ${receivingLine > 0 ? "positive" : "negative"} line is the receiving side — must not read "wins by"`,
      });
    }
  }
  return out;
}
