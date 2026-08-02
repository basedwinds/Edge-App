export interface MarketRow {
  id: number;
  market_type: string;
  source: "kalshi" | "polymarket";
  team: string | null;
  nfl_game_id: string | null;
  game_label: string | null;
  gameday: string | null;
  gametime: string | null; // "HH:MM" local kickoff time, may be blank far out
  line: number | null;
  side: "over" | "under" | null;
  implied_prob: number | null;
  yes_bid: number | null;
  yes_ask: number | null;
  last_price: number | null;
  volume: number | null;
  updated_at: string | null;
  model_prob: number | null;
  model_validated: boolean;
  edge: number | null;
  news_adjustment_pct: number | null;
  news_confidence: "low" | "medium" | "high" | null;
  news_requires_review: boolean;
  final_prob: number | null;
  no_baseline_reason: string | null;
  predicted_home_score: number | null; // only populated on moneyline rows -- combines the spread model's margin + the totals model's total
  predicted_away_score: number | null;
  line_move_pp: number | null; // change in implied_prob over the last 6h -- positive = price for THIS row's team/side has risen
  kelly_fraction: number | null; // quarter-Kelly, capped at 5% of the relevant pool -- see backend app/models/staking.py
  suggested_stake_dollars: number | null;
  suggested_stake_units: number | null;
  stake_pool: "weekly" | "futures" | null;
}

export interface MmaMarketRow {
  id: number;
  market_type: "moneyline" | "distance" | "method_of_victory" | "method_of_finish" | "rounds" | "round_of_victory";
  source: "kalshi" | "polymarket";
  team: string | null; // a fighter's real full name -- no fixed roster, see backend market_catalog_mma.py
  side: "decision" | "kotko" | "submission" | "draw" | "under" | "over" | "other" | null;
  line: number | null; // rounds: round-count threshold; round_of_victory: the round number
  fight_label: string | null;
  mma_fight_id: string | null;
  event_date: string | null; // ISO date
  estimated_start_time: string | null; // full ISO UTC instant, Kalshi's own per-fight occurrence_datetime estimate -- see api/markets.ts::RecommendedBetRow.estimatedStartTime
  weight_class: string | null;
  is_title_bout: boolean;
  scheduled_rounds: number | null;
  implied_prob: number | null;
  yes_bid: number | null;
  yes_ask: number | null;
  last_price: number | null;
  volume: number | null;
  updated_at: string | null;
  model_prob: number | null;
  model_validated: boolean;
  edge: number | null;
  no_baseline_reason: string | null;
  kelly_fraction: number | null;
  suggested_stake_dollars: number | null;
  suggested_stake_units: number | null;
  stake_pool: "weekly" | "futures" | null;
}

export interface TennisMarketRow {
  id: number;
  market_type: "moneyline" | "set_winner" | "game_spread" | "game_total" | "exact_score" | "set_total" | "total_sets";
  source: "kalshi" | "polymarket";
  team: string | null; // a player's real full name -- no fixed roster, see backend market_catalog_tennis.py
  line: number | null; // set_winner: set number; game_spread/game_total/set_total/total_sets: the line
  side: string | null; // exact_score: "{player_sets}-{opponent_sets}" e.g. "2-1"; set_total: "set_N"
  match_label: string | null;
  tennis_match_id: number | null;
  tour: "atp" | "wta" | null;
  tier: "tour" | "challenger" | "itf" | null;
  match_date: string | null; // ISO date
  surface: string | null;
  estimated_start_time: string | null; // full ISO UTC instant, live rows only -- see backend TennisMatch.estimated_start_time
  implied_prob: number | null;
  yes_bid: number | null;
  yes_ask: number | null;
  last_price: number | null;
  volume: number | null;
  updated_at: string | null;
  model_prob: number | null;
  model_validated: boolean;
  edge: number | null;
  no_baseline_reason: string | null;
  kelly_fraction: number | null;
  suggested_stake_dollars: number | null;
  suggested_stake_units: number | null;
  stake_pool: "weekly" | "futures" | null;
}

export interface SoccerMarketRow {
  id: number;
  market_type:
    | "moneyline_3way" | "game_spread" | "game_total" | "btts" | "team_total" | "ftts" | "correct_score"
    | "first_half_winner" | "first_half_spread" | "first_half_total" | "first_half_team_total" | "first_half_btts"
    | "second_half_winner" | "second_half_spread" | "second_half_total" | "second_half_team_total" | "second_half_btts";
  source: "kalshi" | "polymarket";
  team: string | null; // moneyline/half-winner home/away rows, spread, team_total, ftts home/away: real team name; null for draw/none rows and game-level markets (total, btts, correct_score)
  side: "home" | "draw" | "away" | "over" | "yes" | "none" | null; // "over" for total-shaped ladders; "yes" for btts; "none" for ftts's "neither team scores" case
  line: number | null; // spread/total/team_total (and their half variants): the real threshold; unused for moneyline_3way/ftts/correct_score
  correct_score_home: number | null; // correct_score only
  correct_score_away: number | null; // correct_score only
  match_label: string | null; // "{home_team} vs {away_team}"
  soccer_match_id: number | null;
  league: string | null; // football-data.co.uk division code (E0/SP1/I1/D1/F1) or "MLS"
  season: string | null;
  match_date: string | null; // ISO date
  estimated_start_time: string | null; // full ISO UTC instant, live rows only -- see backend SoccerMatch.estimated_start_time
  implied_prob: number | null;
  yes_bid: number | null;
  yes_ask: number | null;
  last_price: number | null;
  volume: number | null;
  updated_at: string | null;
  model_prob: number | null;
  model_validated: boolean;
  edge: number | null;
  no_baseline_reason: string | null;
  kelly_fraction: number | null;
  suggested_stake_dollars: number | null;
  suggested_stake_units: number | null;
  stake_pool: "weekly" | "futures" | null;
  news_adjustment_pct: number | null; // moneyline_3way only -- free injury (Transfermarkt) + motivation (ESPN standings) blend, home-perspective pp
}

export interface ValorantMarketRow {
  id: number;
  market_type: "map_winner" | "series_winner" | "series_handicap" | "series_total" | "tournament_winner";
  source: "kalshi" | "polymarket";
  team: string | null; // real team name; null for series_total (game-level) and tournament_winner's group row
  side: "over" | null; // series_total only
  line: number | null; // map_winner: the map number; series_handicap: map-margin threshold; series_total: total-maps threshold
  match_label: string | null; // "{team_a} vs {team_b}"
  valorant_match_id: number | null;
  event_name: string | null;
  match_date: string | null;
  estimated_start_time: string | null;
  best_of: number | null;
  group_label: string | null; // tournament_winner only
  implied_prob: number | null;
  yes_bid: number | null;
  yes_ask: number | null;
  last_price: number | null;
  volume: number | null;
  updated_at: string | null;
  model_prob: number | null;
  model_validated: boolean;
  edge: number | null;
  no_baseline_reason: string | null;
  kelly_fraction: number | null;
  suggested_stake_dollars: number | null;
  suggested_stake_units: number | null;
  stake_pool: "weekly" | "futures" | null;
}

export interface Cs2MarketRow {
  id: number;
  market_type: "map_winner" | "series_winner" | "series_total" | "tournament_winner";
  source: "kalshi";
  team: string | null; // real team name; null for series_total (game-level) and tournament_winner's group row
  side: "over" | null; // series_total only
  line: number | null; // map_winner: the map number; series_total: total-maps threshold
  match_label: string | null; // "{team_a} vs {team_b}"
  cs2_match_id: number | null;
  event_name: string | null;
  match_date: string | null;
  estimated_start_time: string | null;
  best_of: number | null;
  group_label: string | null; // tournament_winner only
  implied_prob: number | null;
  yes_bid: number | null;
  yes_ask: number | null;
  last_price: number | null;
  volume: number | null;
  updated_at: string | null;
  model_prob: number | null;
  model_validated: boolean;
  edge: number | null;
  no_baseline_reason: string | null;
  kelly_fraction: number | null;
  suggested_stake_dollars: number | null;
  suggested_stake_units: number | null;
  stake_pool: "weekly" | "futures" | null;
}

export interface LolMarketRow {
  id: number;
  market_type: "map_winner" | "series_total" | "tournament_winner";
  source: "kalshi";
  team: string | null; // real team name; null for series_total (game-level) and tournament_winner's group row
  side: "over" | null; // series_total only
  line: number | null; // map_winner: the map number; series_total: total-maps threshold
  match_label: string | null; // "{team_a} vs {team_b}"
  lol_match_id: number | null;
  event_name: string | null;
  match_date: string | null;
  estimated_start_time: string | null;
  best_of: number | null;
  group_label: string | null; // tournament_winner only
  implied_prob: number | null;
  yes_bid: number | null;
  yes_ask: number | null;
  last_price: number | null;
  volume: number | null;
  updated_at: string | null;
  model_prob: number | null;
  model_validated: boolean;
  edge: number | null;
  no_baseline_reason: string | null;
  kelly_fraction: number | null;
  suggested_stake_dollars: number | null;
  suggested_stake_units: number | null;
  stake_pool: "weekly" | "futures" | null;
}

export interface NbaMarketRow {
  id: number;
  market_type: "moneyline" | "spread" | "total" | "team_total" | "spread_1h" | "spread_2h" | "total_1h" | "total_2h";
  source: "kalshi" | "polymarket";
  team: string | null;
  game_label: string | null;
  nba_game_id: string | null;
  gameday: string | null;
  gametime: string | null; // "HH:MM" UTC tip-off time, may be blank far out
  game_type: string | null; // SUMMER | PRE | REG | PLAYIN | POST
  line: number | null;
  side: "over" | "under" | null;
  no_baseline_reason: string | null;
  implied_prob: number | null;
  last_price: number | null;
  volume: number | null;
  updated_at: string | null;
  model_prob: number | null;
  model_validated: boolean;
  edge: number | null;
  news_adjustment_pct: number | null;
  news_confidence: "low" | "medium" | "high" | null;
  news_requires_review: boolean;
  final_prob: number | null;
  kelly_fraction: number | null;
  suggested_stake_dollars: number | null;
  suggested_stake_units: number | null;
  stake_pool: "weekly" | "futures" | null;
}

export interface WnbaMarketRow {
  id: number;
  market_type: "moneyline" | "spread";
  source: "kalshi" | "polymarket";
  team: string | null;
  // Threshold for spread rows (null on moneyline). WnbaMarketOut gained this
  // when spread pricing shipped but this type didn't, so the whole frontend was
  // structurally blind to it and every spread bet rendered with no number --
  // unactionable, since "DAL" without "-3.5" isn't a placeable bet.
  line: number | null;
  game_label: string | null;
  wnba_game_id: string | null;
  gameday: string | null;
  gametime: string | null; // "HH:MM" UTC tip-off time
  game_type: string | null; // PRE | REG | POST
  no_baseline_reason: string | null;
  implied_prob: number | null;
  last_price: number | null;
  volume: number | null;
  updated_at: string | null;
  model_prob: number | null;
  model_validated: boolean;
  edge: number | null;
  kelly_fraction: number | null;
  suggested_stake_dollars: number | null;
  suggested_stake_units: number | null;
  stake_pool: "weekly" | "futures" | null;
}

export interface MlbMarketRow {
  id: number;
  market_type: "moneyline" | "spread" | "total" | "team_total" | "f5" | "rfi";
  source: "kalshi" | "polymarket";
  team: string | null;
  game_label: string | null;
  mlb_game_id: string | null;
  gameday: string | null;
  gametime: string | null; // "HH:MM" UTC start time, may be blank far out
  game_type: string | null; // R | S | F | D | L | W | A
  home_probable_pitcher: string | null;
  away_probable_pitcher: string | null;
  line: number | null;
  /** "over"/"under" for total/team_total; "yes"/"no" for rfi ("yes" = a run
   * scores in the 1st inning); "tie" for f5's draw outcome (team is null then). */
  side: "over" | "under" | "yes" | "no" | "tie" | null;
  no_baseline_reason: string | null;
  implied_prob: number | null;
  last_price: number | null;
  volume: number | null;
  updated_at: string | null;
  news_adjustment_pct: number | null;
  news_confidence: "low" | "medium" | "high" | null;
  news_requires_review: boolean;
  final_prob: number | null;
  model_prob: number | null;
  model_validated: boolean;
  edge: number | null;
  kelly_fraction: number | null;
  suggested_stake_dollars: number | null;
  suggested_stake_units: number | null;
  stake_pool: "weekly" | "futures" | null;
}

export interface FuturesMarketRow {
  id: number;
  market_type: string; // division_winner | conference_champion | one_seed | super_bowl_champion | playoff_qualifier | best_record | undefeated_season | win_total | exact_win_total | wins_any
  source: "kalshi" | "polymarket";
  team: string | null; // null for the league-wide undefeated_season/wins_any markets
  group_label: string | null;
  line: number | null; // win-total ladder markets only (win_total/exact_win_total/wins_any); null otherwise
  side: string | null;
  implied_prob: number | null;
  volume: number | null;
  updated_at: string | null;
  model_prob: number | null;
  model_validated: boolean;
  edge: number | null;
  kelly_fraction: number | null;
  suggested_stake_dollars: number | null;
  suggested_stake_units: number | null;
  stake_pool: "weekly" | "futures" | null;
  line_move_pp: number | null;
  model_note?: string | null; // esports tournament sim: "Elo-approximated bracket, tracking-only" caveat
}

export interface GameMarketRow {
  key: string;
  nfl_game_id: string;
  game_label: string;
  gameday: string;
  source: "kalshi" | "polymarket";
  homeTeam: string;
  homeImpliedProb: number | null;
  homeModelProb: number | null; // raw Elo baseline, no news
  homeFinalProb: number | null; // Elo + news adjustment, when news is cached
  homeBestProb: number | null; // homeFinalProb ?? homeModelProb -- the "current best estimate" for display
  edge: number | null; // homeBestProb vs market, so it reflects news when present
  volume: number | null;
  modelValidated: boolean;
  hasNews: boolean;
  newsConfidence: "low" | "medium" | "high" | null;
  newsRequiresReview: boolean;
  noBaselineReason: string | null;
  predictedHomeScore: number | null;
  predictedAwayScore: number | null;
  lineMovePp: number | null;
  kellyFraction: number | null;
  suggestedStakeDollars: number | null;
  suggestedStakeUnits: number | null;
  stakePool: "weekly" | "futures" | null;
}
