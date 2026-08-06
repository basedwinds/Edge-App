/** One place that turns a market row into English, shared by every surface that
 * shows a pick: the cross-sport Recommended table, the per-sport Recommended
 * pages, and the Bet Tracker.
 *
 * It used to live inside RecommendedBetsTable, so the tracker printed raw
 * market_type strings instead ("set_spread · Marta Kostyuk -1.5", "series_total
 * over 2.5"). Copying it would have made a fourth renderer to keep in sync --
 * there were already three, and they had already drifted apart twice (tennis
 * spreads, esports map_winner). It takes a structural type rather than
 * RecommendedBetRow so a placed-bet payload can use it directly.
 */
import { MARKET_TYPE_LABELS } from "../components/markets/FuturesTable";
import { describeTennisSpread, TENNIS_MARKET_TYPE_LABELS } from "./tennisLabel";

export type PickLike = {
  marketType: string;
  team: string | null;
  side: string | null;
  line: number | null;
  sport: string;
  correctScoreHome?: number | null;
  correctScoreAway?: number | null;
};

const GAME_MARKET_TYPE_LABELS: Record<string, string> = {
  moneyline: "Moneyline",
  spread: "Spread",
  total: "Total",
  team_total: "Team Total",
  spread_1h: "1st Half Spread",
  spread_2h: "2nd Half Spread",
  total_1h: "1st Half Total",
  total_2h: "2nd Half Total",
  f5: "First 5 Innings",
  rfi: "Run in 1st Inning",
  distance: "Goes the Distance",
  method_of_victory: "Method of Victory",
  method_of_finish: "Method of Finish",
  rounds: "Round of Finish",
  round_of_victory: "Round of Victory",
  // Soccer (added 2026-07-19) -- own distinct market_type strings, not
  // reusable with NFL/NBA/MLB's bare "spread"/"total"/"moneyline" above.
  moneyline_3way: "Moneyline",
  game_spread: "Spread",
  game_total: "Total",
  btts: "BTTS",
  ftts: "1st To Score",
  correct_score: "Correct Score",
  first_half_winner: "1H Winner",
  first_half_spread: "1H Spread",
  first_half_total: "1H Total",
  first_half_team_total: "1H Team Total",
  first_half_btts: "1H BTTS",
  second_half_winner: "2H Winner",
  second_half_spread: "2H Spread",
  second_half_total: "2H Total",
  second_half_team_total: "2H Team Total",
  second_half_btts: "2H BTTS",
  // Esports (Valorant/CS2/LoL) -- fell through to the raw market_type
  // string until now (found live 2026-07-20 while adding real coverage for
  // all 3 titles' own KXGAME series winner tickers), same wording as each
  // sport's own Dashboard page (Cs2.tsx/Valorant.tsx/Lol.tsx's own
  // MARKET_TYPE_LABELS).
  map_winner: "Map Winner",
  series_winner: "Series Winner",
  series_total: "Total Maps",
  series_handicap: "Map Handicap",
  // Racing (F1/NASCAR/IndyCar). These had no entry anywhere on the frontend,
  // so the tracker printed the bare "top_n" / "constructor_pole". Names
  // mirror racing_markets.py::_MT_LABEL so the two agree.
  race_winner: "Race Winner",
  pole: "Pole Position",
  top_n: "Top-N Finish",
  h2h: "Head-to-Head",
  constructor_pole: "Constructor Pole",
  drivers_champion: "Drivers' Champion",
  constructors_champion: "Constructors' Champion",
  // The last of the untitled types, found by checking marketTypeLabel against
  // every (sport, market_type) pair in the database rather than by eye.
  tournament_winner: "Tournament Winner",
  team_points: "Season Points",            // soccer KX*TEAMPOINTS ladder
  // CFB's playoff family. Meanings read off cfb_markets.py: a conference
  // QUALIFIER is finishing top two ("Reaching a conference title game IS
  // finishing top two"), regtop is a regular-season top-N with the depth on
  // `line`, and title_conference asks which CONFERENCE produces the champion
  // -- not which team, so it is deliberately not "Conference Champion".
  conference_qualifier: "Conference Title Game",
  conference_regtop: "Conference Reg. Season",
  cfb_playoff: "Make Playoff",
  cfb_quarterfinal: "Reach Quarterfinal",
  cfb_title_conference: "Champion's Conference",
};

// Tennis is checked FIRST because it collides with soccer on two keys:
// `game_spread`/`game_total` mean goals there and GAMES here, and tennis
// additionally owns set_spread/set_winner/set_total/total_sets, none of
// which had a name at all -- so a handicap row printed the raw
// "set_spread" and read as "over -1.5 set spread" (user-reported).
export function marketTypeLabel(marketType: string, sport: string): string {
  if (sport === "tennis" && TENNIS_MARKET_TYPE_LABELS[marketType]) {
    return TENNIS_MARKET_TYPE_LABELS[marketType];
  }
  return GAME_MARKET_TYPE_LABELS[marketType] ?? MARKET_TYPE_LABELS[marketType] ?? marketType;
}

// Spells out exactly what "Yes"/winning this bet means, in plain English --
// added 2026-07-17 after user feedback that the old "team ± line" rendering
// was genuinely ambiguous. The underlying `line` field means "this team's
// OWN margin must exceed `line`" for every spread-shaped market in this app
// (see game_lines.py/game_lines_nba.py/game_lines_mlb.py's prob_team_covers
// -- same convention on all three sports), which does NOT match standard
// bookmaker "-1.5 favorite / +1.5 underdog" notation: a favorite's line here
// is POSITIVE (must win by more than N), an underdog's is NEGATIVE (must
// not lose by N or more). Showing the raw signed number as "TEAM -2.5"
// reads backwards to anyone using normal sports-betting intuition, so this
// spells out the real threshold in words instead of leaning on a sign
// convention at all.
function describeSpreadPick(team: string, line: number): string {
  if (line > 0) return `${team} wins by ${Math.ceil(line)}+`;
  if (line < 0) return `${team} doesn't lose by ${Math.ceil(Math.abs(line))}+`;
  return `${team} wins outright`;
}

export function describePick(row: PickLike): string {
  const { team, line, side, marketType, sport } = row;
  if (marketType === "f5") return side === "tie" ? "Tie after 5 innings" : `${team ?? "—"} wins first 5 innings`;
  if (marketType === "rfi") return side === "no" ? "No run in the 1st inning" : "A run scores in the 1st inning";
  if (marketType === "distance") return "Fight goes the distance";
  if (marketType === "method_of_finish") {
    return { kotko: "KO/TKO", submission: "Submission", decision: "Decision" }[side ?? ""] ?? side ?? "—";
  }
  if (marketType === "rounds" && line !== null) {
    return side === "under" ? `Ends before round ${line}` : `Goes past round ${line}`;
  }
  // Tennis's two spreads, which disagree on what a negative line means -- see
  // describeTennisSpread. set_spread had NO branch at all before, so it fell
  // through to the Over/Under fallback at the bottom and rendered as
  // "Marta Kostyuk Over -1.5" (user-reported 2026-08-04): a handicap read as
  // a total, with the sign left for the reader to interpret. There is no sign
  // convention that makes that line correct, because the two markets use
  // opposite ones.
  if (sport === "tennis") {
    const spread = describeTennisSpread(marketType, team, line);
    if (spread) return spread;
  }
  if (marketType === "set_winner" && line !== null) return `${team ?? "—"} wins Set ${Math.round(line)}`;
  if (marketType === "exact_score" && side) return `${team ?? "—"} wins ${side}`;
  // REAL BUG fixed here (caught live via user report: "over 3.5 games" shown
  // for a Soccer total): "game_total" is used by BOTH Tennis (total GAMES
  // across a set/match) and Soccer (total GOALS in a match) -- two sports
  // sharing the identical market_type string for genuinely different real
  // quantities. Every other consumer of this string already disambiguates by
  // sport (e.g. buildSoccerRecommendedBets' own SOCCER_LADDER_TYPES), this
  // label just never did.
  if (marketType === "game_total" && line !== null) return `Over ${line} ${sport === "soccer" ? "goals" : "games"}`;
  // Second batch (added 2026-07-19) -- Soccer's own FTTS/correct_score/
  // half-family market types, none of which fit any of the generic
  // patterns below (FTTS/half-winner are 3-way-but-not-moneyline_3way;
  // correct_score needs the real scoreline, which lives in its own two
  // fields, not team/line/side; half-spread doesn't match the plain
  // "spread"-prefix check below since its market_type string is
  // "first_half_spread"/"second_half_spread", not "spread_...").
  if (marketType === "btts" || marketType === "first_half_btts" || marketType === "second_half_btts") {
    const halfNote = marketType === "first_half_btts" ? " (1st half)" : marketType === "second_half_btts" ? " (2nd half)" : "";
    return `Both teams to score${halfNote}`;
  }
  if (marketType === "ftts") return side === "none" ? "Neither team scores first" : (team ?? "—");
  if (marketType === "correct_score") {
    return row.correctScoreHome != null && row.correctScoreAway != null
      ? `${row.correctScoreHome} - ${row.correctScoreAway}`
      : "—";
  }
  if (marketType === "moneyline_3way" || marketType === "first_half_winner" || marketType === "second_half_winner") {
    const halfNote = marketType === "first_half_winner" ? " (1st half)" : marketType === "second_half_winner" ? " (2nd half)" : "";
    return (side === "draw" ? "Draw" : (team ?? "—")) + halfNote;
  }
  if ((marketType === "first_half_spread" || marketType === "second_half_spread") && line !== null && team) {
    const halfNote = marketType === "first_half_spread" ? " (1st half)" : " (2nd half)";
    return `${team} wins by ${Math.ceil(line)}+ goals${halfNote}`;
  }
  if (marketType === "set_total" && line !== null) {
    const setLabel = side ? side.replace("set_", "Set ") : "?";
    return `${setLabel}: Over ${line} games`;
  }
  if (marketType === "total_sets" && line !== null) return `Over ${line} sets`;
  // REAL BUG fixed here (caught live via user report: "B2U over 1" shown for
  // a LoL map-winner pick, genuinely unreadable): this table's generic
  // fallback below (built for NFL/NBA/MLB's plain team+line markets) doesn't
  // know about the 3 esports titles' own map_winner/series_handicap/
  // series_total market types, so it silently mislabeled all three as a
  // bare "team Over/Under line" -- the individual sport Dashboard pages
  // (Lol.tsx/Cs2.tsx/Valorant.tsx's own formatPick) already spell these out
  // correctly, this table's shared describePick just never got the same 3
  // cases. map_winner's `line` is a map NUMBER (not a threshold), so
  // "Over"/"Under" is nonsensical for it regardless of source sport.
  // top_n's `line` is a finishing POSITION, not a threshold: racing_markets.py
  // reads it as sim[driver][`top${line}`], i.e. P(finishes in the top N). The
  // generic Over/Under fallback rendered it "Oscar Piastri Over 3", which
  // reads as a total and points the wrong way -- finishing "over" 3rd is the
  // losing side of this bet.
  if (marketType === "team_points" && line !== null) return `${team ?? "—"} gets ${Math.round(line)}+ points`;
  if (marketType === "conference_regtop" && line !== null) return `${team ?? "—"} finishes top ${Math.round(line)}`;
  if (marketType === "top_n" && line !== null) return `${team ?? "—"} finishes top ${Math.round(line)}`;
  if (marketType === "race_winner" || marketType === "pole" || marketType === "constructor_pole") return team ?? "—";
  if (marketType === "map_winner" && line !== null) return `${team ?? "—"} wins Map ${Math.round(line)}`;
  // series_handicap (CS2 + Valorant, from Polymarket).
  //
  // THE SIGN IS THE OPPOSITE OF describeSpreadPick's, and this used to claim
  // otherwise -- the old comment here said "same signed-margin convention as
  // describeSpreadPick", which is exactly the mistake. They differ in the
  // model:
  //
  //   spread          prob_team_covers  -> P(margin >  line)   positive = wins by
  //   series_handicap prob_handicap_cover_a(line)
  //                                     -> P(a - b > -line)    sign FLIPPED
  //
  // So a NEGATIVE handicap line is the team giving maps (must win by that
  // margin) and a POSITIVE line is the team receiving them.
  //
  // REAL BUG this fixes (user-reported 2026-08-06, "Galorys vs BORRACHEIROS ...
  // it has me placing BORRACHEIROS wins by 2+ maps, but the explanation has
  // Galorys with a higher elo"). The row is BORRACHEIROS at line +1.5, which
  // the model prices at 0.6622 -- that is "BORRACHEIROS avoids a 0-2 sweep",
  // i.e. wins at least one map. It was rendered as "wins by 2+ maps", which is
  // the -1.5 side and worth 0.1754. The label described the OPPOSITE bet, and
  // made a sensible 66% pick read as an implausible longshot against a
  // stronger opponent. The recommendation and the staking were correct
  // throughout; only this string was wrong.
  if (marketType === "series_handicap" && line !== null && team) {
    if (line < 0) return `${team} wins by ${Math.ceil(Math.abs(line))}+ maps`;
    if (line > 0) return `${team} doesn't lose by ${Math.ceil(line)}+ maps`;
    return `${team} wins outright`;
  }
  if (marketType === "series_total" && line !== null) return `${side === "under" ? "Under" : "Over"} ${line} maps`;
  if (line === null) return team ?? "—";
  if (marketType.startsWith("spread") || marketType === "game_spread") {
    if (sport === "soccer" && team) return `${team} wins by ${Math.ceil(line)}+ goals`;
    return team ? describeSpreadPick(team, line) : String(line);
  }
  // total/team_total/half-totals AND every remaining ladder market (win_total,
  // season-stat thresholds, division win-totals) all resolve on an Over/Under
  // (or "at least N") threshold -- `side` is "over" for every one of these
  // except real Polymarket/Kalshi totals, which can genuinely be "under".
  const sideLabel = side === "under" ? "Under" : "Over";
  return team ? `${team} ${sideLabel} ${line}` : `${sideLabel} ${line}`;
}
