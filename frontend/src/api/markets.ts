import { apiGet, apiPost, apiPut, apiDelete } from "./client";
import type { Cs2MarketRow, FuturesMarketRow, GameMarketRow, LolMarketRow, MarketRow, MlbMarketRow, MmaMarketRow, NbaMarketRow, SoccerMarketRow, TennisMarketRow, ValorantMarketRow, WnbaMarketRow } from "../types/market";

export async function fetchMarkets(): Promise<MarketRow[]> {
  return apiGet<MarketRow[]>("/markets");
}

export async function fetchFutures(): Promise<FuturesMarketRow[]> {
  return apiGet<FuturesMarketRow[]>("/markets/futures");
}

export async function fetchNbaMarkets(): Promise<NbaMarketRow[]> {
  return apiGet<NbaMarketRow[]>("/nba/markets");
}

export async function fetchNbaFutures(): Promise<FuturesMarketRow[]> {
  return apiGet<FuturesMarketRow[]>("/nba/futures");
}

export async function fetchWnbaMarkets(): Promise<WnbaMarketRow[]> {
  return apiGet<WnbaMarketRow[]>("/wnba/markets");
}

export async function fetchMlbMarkets(): Promise<MlbMarketRow[]> {
  return apiGet<MlbMarketRow[]>("/mlb/markets");
}

export async function fetchMlbFutures(): Promise<FuturesMarketRow[]> {
  return apiGet<FuturesMarketRow[]>("/mlb/futures");
}

export async function fetchMmaMarkets(): Promise<MmaMarketRow[]> {
  return apiGet<MmaMarketRow[]>("/mma/markets");
}

export async function fetchTennisMarkets(): Promise<TennisMarketRow[]> {
  return apiGet<TennisMarketRow[]>("/tennis/markets");
}

export async function fetchTennisFutures(): Promise<FuturesMarketRow[]> {
  return apiGet<FuturesMarketRow[]>("/tennis/futures");
}

export async function fetchSoccerMarkets(): Promise<SoccerMarketRow[]> {
  return apiGet<SoccerMarketRow[]>("/soccer/markets");
}

export async function fetchSoccerFutures(): Promise<FuturesMarketRow[]> {
  return apiGet<FuturesMarketRow[]>("/soccer/futures");
}

export async function fetchValorantMarkets(): Promise<ValorantMarketRow[]> {
  return apiGet<ValorantMarketRow[]>("/valorant/markets");
}

export async function fetchValorantFutures(): Promise<FuturesMarketRow[]> {
  return apiGet<FuturesMarketRow[]>("/valorant/futures");
}

export async function fetchCs2Markets(): Promise<Cs2MarketRow[]> {
  return apiGet<Cs2MarketRow[]>("/cs2/markets");
}

export async function fetchCs2Futures(): Promise<FuturesMarketRow[]> {
  return apiGet<FuturesMarketRow[]>("/cs2/futures");
}

export interface RacingMarketRow {
  id: number;
  series: "f1" | "irl" | "nascar";
  source: "kalshi" | "polymarket";
  event: string | null;
  market_type: "race_winner" | "top_n" | "pole";
  line: number | null;
  driver: string;
  implied_prob: number | null;
  model_prob: number | null;
  model_validated: boolean;
  edge: number | null;
  volume: number | null;
  close_time: string | null;
  model_note?: string | null;
  kelly_fraction?: number | null;
  suggested_stake_dollars?: number | null;
  suggested_stake_units?: number | null;
  stake_pool?: string | null;
}

export async function markRacingBetPlaced(row: RacingMarketRow, stakeDollars: number, stakeUnits: number | null): Promise<PlacedBetPayload> {
  const mt = row.market_type === "top_n" ? `Top ${row.line}` : row.market_type === "pole" ? "Pole" : "Race Winner";
  return apiPost<PlacedBetPayload>("/placed-bets", {
    market_id: row.id,
    market_type: row.market_type,
    source: row.source,
    sport: row.series,
    team: row.driver,
    line: row.line,
    side: null,
    label: `${row.driver} — ${mt}`,
    stake_pool: "weekly",
    stake_dollars: stakeDollars,
    stake_units: stakeUnits,
    market_prob_at_placement: row.implied_prob,
    model_prob_at_placement: row.model_prob,
    edge_at_placement: row.edge,
  });
}

export async function fetchRacingMarkets(): Promise<RacingMarketRow[]> {
  return apiGet<RacingMarketRow[]>("/racing/markets");
}

export async function fetchLolMarkets(): Promise<LolMarketRow[]> {
  return apiGet<LolMarketRow[]>("/lol/markets");
}

export async function fetchLolFutures(): Promise<FuturesMarketRow[]> {
  return apiGet<FuturesMarketRow[]>("/lol/futures");
}

export interface DivergenceRow {
  sport: string;
  entity_id: string;
  market_type: string;
  team: string | null;
  line: number | null;
  side: string | null;
  kalshi_prob: number;
  polymarket_prob: number;
  gap: number;
  buy_side: "kalshi" | "polymarket";
  kalshi_market_id: number;
  polymarket_market_id: number;
  kalshi_volume: number | null;
  polymarket_volume: number | null;
}

export async function fetchDivergences(minGap = 0.03): Promise<DivergenceRow[]> {
  return apiGet<DivergenceRow[]>(`/markets/cross-platform-divergences?min_gap=${minGap}`);
}

export interface ClvBucketRow {
  sport: string;
  market_type: string;
  n: number;
  avg_clv_pp: number;
  enabled: boolean;
  status: string;
}

export async function fetchClvBuckets(): Promise<ClvBucketRow[]> {
  return apiGet<ClvBucketRow[]>("/placed-bets/clv-buckets");
}

export interface PaperSummary {
  total: number;
  pending: number;
  settled: number;
  with_clv: number;
  by_sport: Record<string, number>;
}

export async function fetchPaperSummary(): Promise<PaperSummary> {
  return apiGet<PaperSummary>("/placed-bets/paper-summary");
}

export async function refreshNews(nflGameId: string): Promise<{ status: string }> {
  return apiPost(`/markets/${encodeURIComponent(nflGameId)}/refresh-news`);
}

export interface HealthPayload {
  status: string;
  data_dir: string;
  last_refresh_at: string | null;
}

export async function fetchHealth(): Promise<HealthPayload> {
  return apiGet<HealthPayload>("/health");
}

export async function triggerRefresh(): Promise<{ status: string }> {
  return apiPost("/markets/refresh");
}

export interface ReasoningFactorPayload {
  label: string;
  detail: string;
}

export interface ReasoningPayload {
  market_id: number;
  market_type: string;
  label: string;
  model_prob: number | null;
  market_prob: number | null;
  edge: number | null;
  insight: string;
  methodology: string;
  factors: ReasoningFactorPayload[];
  caveats: string[];
}

export async function fetchMarketReasoning(
  marketId: number,
  modelProb: number | null,
  marketProb: number | null,
  sport: "nfl" | "nba" | "wnba" | "mlb" | "mma" | "tennis" | "soccer" | "valorant" | "cs2" | "lol" | "f1" | "nascar" | "irl" = "nfl"
): Promise<ReasoningPayload> {
  const params = new URLSearchParams();
  if (modelProb !== null) params.set("model_prob", String(modelProb));
  if (marketProb !== null) params.set("market_prob", String(marketProb));
  const path =
    sport === "f1" || sport === "nascar" || sport === "irl" ? `/racing/markets/${marketId}/reasoning`
    : sport === "nba" ? `/nba/markets/${marketId}/reasoning`
    : sport === "wnba" ? `/wnba/markets/${marketId}/reasoning`
    : sport === "mlb" ? `/mlb/markets/${marketId}/reasoning`
    : sport === "mma" ? `/mma/markets/${marketId}/reasoning`
    : sport === "tennis" ? `/tennis/markets/${marketId}/reasoning`
    : sport === "soccer" ? `/soccer/markets/${marketId}/reasoning`
    : sport === "valorant" ? `/valorant/markets/${marketId}/reasoning`
    : sport === "cs2" ? `/cs2/markets/${marketId}/reasoning`
    : sport === "lol" ? `/lol/markets/${marketId}/reasoning`
    : `/markets/${marketId}/reasoning`;
  return apiGet<ReasoningPayload>(`${path}?${params.toString()}`);
}

export interface SettingsPayload {
  bankroll_dollars: number;
  bankroll_units: number;
  nfl_allocation_pct: number;
  futures_subpool_pct: number;
  nba_allocation_pct: number;
  nba_futures_subpool_pct: number;
  fractional_kelly: number;
  max_stake_fraction: number;
  min_edge_to_bet: number;
  unit_dollars: number;
  nfl_pool_dollars: number;
  futures_pool_dollars: number;
  weekly_pool_dollars: number;
  nba_pool_dollars: number;
  nba_futures_pool_dollars: number;
  nba_weekly_pool_dollars: number;
  wnba_allocation_pct: number;
  wnba_futures_subpool_pct: number;
  wnba_pool_dollars: number;
  wnba_futures_pool_dollars: number;
  wnba_weekly_pool_dollars: number;
  total_allocation_pct: number;
  mlb_allocation_pct: number;
  mlb_futures_subpool_pct: number;
  mlb_pool_dollars: number;
  mlb_futures_pool_dollars: number;
  mlb_weekly_pool_dollars: number;
  mma_allocation_pct: number;
  mma_futures_subpool_pct: number;
  mma_pool_dollars: number;
  mma_futures_pool_dollars: number;
  mma_weekly_pool_dollars: number;
  tennis_allocation_pct: number;
  tennis_futures_subpool_pct: number;
  tennis_pool_dollars: number;
  tennis_futures_pool_dollars: number;
  tennis_weekly_pool_dollars: number;
  soccer_allocation_pct: number;
  soccer_futures_subpool_pct: number;
  soccer_pool_dollars: number;
  soccer_futures_pool_dollars: number;
  soccer_weekly_pool_dollars: number;
  valorant_allocation_pct: number;
  valorant_futures_subpool_pct: number;
  valorant_pool_dollars: number;
  valorant_futures_pool_dollars: number;
  valorant_weekly_pool_dollars: number;
  cs2_allocation_pct: number;
  cs2_futures_subpool_pct: number;
  cs2_pool_dollars: number;
  cs2_futures_pool_dollars: number;
  cs2_weekly_pool_dollars: number;
  lol_allocation_pct: number;
  lol_futures_subpool_pct: number;
  lol_pool_dollars: number;
  lol_futures_pool_dollars: number;
  lol_weekly_pool_dollars: number;
}

export async function fetchSettings(): Promise<SettingsPayload> {
  return apiGet<SettingsPayload>("/settings");
}

export async function updateSettings(input: {
  bankrollDollars: number;
  bankrollUnits: number;
  nflAllocationPct: number;
  futuresSubpoolPct: number;
  nbaAllocationPct: number;
  nbaFuturesSubpoolPct: number;
  wnbaAllocationPct: number;
  wnbaFuturesSubpoolPct: number;
  mlbAllocationPct: number;
  mlbFuturesSubpoolPct: number;
  mmaAllocationPct: number;
  mmaFuturesSubpoolPct: number;
  tennisAllocationPct: number;
  tennisFuturesSubpoolPct: number;
  soccerAllocationPct: number;
  soccerFuturesSubpoolPct: number;
  valorantAllocationPct: number;
  valorantFuturesSubpoolPct: number;
  cs2AllocationPct: number;
  cs2FuturesSubpoolPct: number;
  lolAllocationPct: number;
  lolFuturesSubpoolPct: number;
  fractionalKelly: number;
  maxStakeFraction: number;
  minEdgeToBet: number;
}): Promise<SettingsPayload> {
  return apiPut<SettingsPayload>("/settings", {
    bankroll_dollars: input.bankrollDollars,
    bankroll_units: input.bankrollUnits,
    nfl_allocation_pct: input.nflAllocationPct,
    futures_subpool_pct: input.futuresSubpoolPct,
    nba_allocation_pct: input.nbaAllocationPct,
    nba_futures_subpool_pct: input.nbaFuturesSubpoolPct,
    wnba_allocation_pct: input.wnbaAllocationPct,
    wnba_futures_subpool_pct: input.wnbaFuturesSubpoolPct,
    mlb_allocation_pct: input.mlbAllocationPct,
    mlb_futures_subpool_pct: input.mlbFuturesSubpoolPct,
    mma_allocation_pct: input.mmaAllocationPct,
    mma_futures_subpool_pct: input.mmaFuturesSubpoolPct,
    tennis_allocation_pct: input.tennisAllocationPct,
    tennis_futures_subpool_pct: input.tennisFuturesSubpoolPct,
    soccer_allocation_pct: input.soccerAllocationPct,
    soccer_futures_subpool_pct: input.soccerFuturesSubpoolPct,
    valorant_allocation_pct: input.valorantAllocationPct,
    valorant_futures_subpool_pct: input.valorantFuturesSubpoolPct,
    cs2_allocation_pct: input.cs2AllocationPct,
    cs2_futures_subpool_pct: input.cs2FuturesSubpoolPct,
    lol_allocation_pct: input.lolAllocationPct,
    lol_futures_subpool_pct: input.lolFuturesSubpoolPct,
    fractional_kelly: input.fractionalKelly,
    max_stake_fraction: input.maxStakeFraction,
    min_edge_to_bet: input.minEdgeToBet,
  });
}

export interface CatalogEntryPayload {
  id: number;
  platform: "kalshi" | "polymarket";
  identifier: string;
  title: string;
  sport: string;
  first_seen: string;
  category: "news" | "stat_leader" | "futures" | "match_outcome" | "review";
  category_note: string;
  auto_priceable: boolean;
}

export async function fetchNewCatalogEntries(): Promise<CatalogEntryPayload[]> {
  return apiGet<CatalogEntryPayload[]>("/catalog/new");
}

export async function fetchFlaggedCatalogEntries(): Promise<CatalogEntryPayload[]> {
  return apiGet<CatalogEntryPayload[]>("/catalog/flagged");
}

/** disposition: "not_relevant" (reviewed, nothing to build -- the old
 * single "Dismiss" behavior) or "flagged" (reviewed AND worth building --
 * see catalog.py's DismissIn docstring: this does NOT auto-build
 * ingestion, it just keeps the entry in a persistent backlog instead of
 * letting the decision vanish the moment it's dismissed). */
export async function dismissCatalogEntry(id: number, disposition: "not_relevant" | "flagged" = "not_relevant"): Promise<{ status: string }> {
  return apiPost(`/catalog/${id}/dismiss`, { disposition });
}

export async function resolveFlaggedCatalogEntry(id: number): Promise<{ status: string }> {
  return apiPost(`/catalog/${id}/resolve`);
}

export interface RecommendedBetRow {
  key: string;
  marketId: number;
  label: string;
  marketType: string;
  team: string | null;
  line: number | null;
  side: string | null;
  /** Soccer's correct_score market_type only -- the real scoreline this
   * row's YES side resolves on. Optional since every other sport/market
   * type never sets it. */
  correctScoreHome?: number | null;
  correctScoreAway?: number | null;
  /** ISO date, game-tied (weekly-pool) rows only -- null for futures/season-long rows. */
  gameday: string | null;
  /** "HH:MM" local kickoff, may be blank far out even for game-tied rows. */
  gametime: string | null;
  /** MMA only: full ISO UTC instant (Kalshi's own per-fight `occurrence_datetime`
   * estimate, real and staggered across a card -- not a flat event-level time).
   * Kept separate from gameday/gametime rather than decomposed into them --
   * MMA's gameday is ufcstats' US-local event date, but a fight's real UTC
   * instant is very often on the FOLLOWING UTC calendar day (US evening
   * cards routinely cross UTC midnight), so reconstructing gameday from this
   * instant's own UTC date would show "tomorrow" for nearly every fight --
   * the same class of gameday/gametime-must-share-one-source bug this app's
   * MLB kickoff-instant fix already had to solve once. Using a real,
   * complete instant directly for the TIME portion sidesteps that class of
   * bug entirely instead of re-deriving it. */
  estimatedStartTime: string | null;
  source: "kalshi" | "polymarket";
  impliedProb: number | null;
  estProb: number | null;
  edge: number | null;
  lineMovePp: number | null;
  kellyFraction: number;
  suggestedStakeDollars: number;
  suggestedStakeUnits: number | null;
  stakePool: "weekly" | "futures";
  volume: number | null;
  nflGameId: string | null;
  /** Which sport this row belongs to -- "nfl" for every row built by
   * buildRecommendedBets, "nba" for buildNbaRecommendedBets, "mlb" for
   * buildMlbRecommendedBets, "mma" for buildMmaRecommendedBets, "tennis" for
   * buildTennisRecommendedBets, "soccer" for buildSoccerRecommendedBets,
   * "valorant"/"cs2"/"lol" for buildValorantRecommendedBets/
   * buildCs2RecommendedBets/buildLolRecommendedBets -- each esports title
   * gets its own independent bankroll pool as of 2026-07-20 (see
   * settings.py::VALORANT_ALLOCATION_PCT_KEY), same as every other sport.
   * Threaded through to markBetPlaced so it knows which game-id field to
   * send. */
  sport: "nfl" | "nba" | "wnba" | "mlb" | "mma" | "tennis" | "soccer" | "valorant" | "cs2" | "lol";
  nbaGameId: string | null;
  /** Optional so only the WNBA builder sets it -- every other sport's builder
   * omits it (undefined), avoiding a churn edit across all ~9 builders. */
  wnbaGameId?: string | null;
  mlbGameId: string | null;
  mmaFightId: string | null;
  tennisMatchId: number | null;
  soccerMatchId: number | null;
  valorantMatchId: number | null;
  cs2MatchId: number | null;
  lolMatchId: number | null;
  /** Identifies the underlying real-world proposition, independent of which
   * platform or which threshold rung priced it -- see buildRecommendedBets. */
  groupKey: string;
  /** Non-null when a genuinely unresolved game-time decision exists (a
   * questionable/doubtful starter, an unannounced MLB probable pitcher,
   * whether a team rests starters) -- the model's current number was
   * computed before that resolves, so it may move. Null means nothing
   * currently flagged, NOT "confirmed clean" -- this app doesn't track
   * every possible unknown, only the ones its own situational/injury data
   * actually covers. See RecommendedBetsTable.tsx's Wait badge. */
  waitReason: string | null;
}

export interface RecommendedBetsResult {
  rows: RecommendedBetRow[];
  /** How many candidate rows existed before ladder/cross-platform collapsing
   * -- shown to the user so "722 -> 41" is visible, not just the final 41. */
  rawCandidateCount: number;
  /** Rows that collapsed into the ones shown (same real-world bet, worse
   * rung or worse-priced platform). */
  collapsedCount: number;
  /** Rows that survived collapsing but were cut by the portfolio cap. */
  cutByPortfolioCapCount: number;
}

// Multi-rung "N or more" ladders (win_total, season-stat thresholds,
// division win-total ladders): adjacent rungs are nearly perfectly
// correlated (if a team/player clears the 4500-yard threshold at a real
// edge, it almost always also clears 4000) -- these are the SAME view on
// the same underlying number, not independent opportunities, so only the
// single best-edge rung per (market, team/candidate, source) is kept.
const LADDER_MARKET_TYPES = new Set([
  "win_total",
  "wins_any",
  "division_wins",
  "season_pass_yds",
  "season_rush_yds",
  "season_rec_yds",
  "season_rush_tds",
  "season_rec_tds",
  "season_rec",
]);

// The 6 season-stat categories specifically -- all driven by the same
// underlying player-usage signal, so a single player showing edge across
// several of them gets capped to one bet, not several (see Pass 3 below).
const PLAYER_STAT_MARKET_TYPES = new Set([
  "season_pass_yds",
  "season_rush_yds",
  "season_rec_yds",
  "season_rush_tds",
  "season_rec_tds",
  "season_rec",
]);

// GAME-TIED ladders (spread/total/team_total, real for all three sports --
// confirmed live 2026-07-17 by a user report: MLB team-total rows showed
// "Twins Over 3.5", "Over 4.5", "Over 5.5" for the SAME game all at once).
// Same "adjacent rungs are the same underlying number, not independent
// opportunities" reasoning as LADDER_MARKET_TYPES above, but these need the
// GAME in the collapse key (unlike win_total, which is one season-long
// entity per team) -- two DIFFERENT games' team-total-over bets for the same
// team are genuinely separate propositions and must NOT collapse together.
const GAME_LADDER_MARKET_TYPES = new Set([
  "spread", "total", "team_total",
  "spread_1h", "spread_2h", "total_1h", "total_2h",
]);

/** Key for collapsing multiple LINES of the same real-world "shape" of bet
 * within one game (e.g. every rung of a total's ladder) down to the single
 * best-edge rung. `team` disambiguates each side's own ladder on spread and
 * team_total (a game's two teams are genuinely separate propositions).
 *
 * REAL BUG fixed here (2026-07-19, user report: recommended bets showing
 * both "over" and "under" rungs of the SAME total/spread/team_total
 * simultaneously for one game): `side` used to be part of this key, which
 * collapsed rungs WITHIN a direction (Over 3.5 vs Over 4.5) but let the best
 * Over rung and the best Under rung both survive as separate "opportunities"
 * -- they aren't independent bets, they're two readings of the model's SAME
 * disagreement with the market on one underlying number, so recommending
 * both isn't diversification, it's the same view staked twice (and if the
 * true outcome lands between the two lines, both lose). Dropping `side` so
 * only the single best-edge rung across BOTH directions survives per game/
 * team -- exactly how `recommendedKey` (win_total/wins_any's own ladder key,
 * which never included `side`) already behaved, so this brings the newer
 * game-scoped ladders back in line with the original, correct design. */
function gameLadderKey(row: RecommendedBetRow): string {
  const gameId = row.nflGameId || row.nbaGameId || row.mlbGameId || row.label;
  return `${row.marketType}|${row.source}|${gameId}|${row.team ?? ""}`;
}

/** Picks the right collapse key for Pass 1 below depending on which kind of
 * ladder (if any) this row's market_type is. Shared across all three sports'
 * build*RecommendedBets functions so the fix lands in one place, not three. */
function ladderCollapseKey(row: RecommendedBetRow): string {
  if (LADDER_MARKET_TYPES.has(row.marketType)) return row.groupKey;
  if (GAME_LADDER_MARKET_TYPES.has(row.marketType)) return gameLadderKey(row);
  return row.key;
}

// Cap on how much of EACH pool (weekly / futures -- see backend
// app/models/staking.py::WEEKLY_MARKET_TYPES) this list will suggest
// committing at once, as a fraction of that pool's own dollar amount.
// Raised 0.25 -> 0.6 2026-07-16 alongside the pool split: pools are now
// already a right-sized sub-allocation of a much larger cross-sport
// bankroll (NFL gets its own slice, futures/weekly split further within
// that -- see Settings), so the ceiling within a pool can be more generous
// than when it applied to the whole bankroll. Two SEPARATE caps (not one
// shared number) because kelly_fraction is a fraction of a DIFFERENT base
// depending on which pool a row belongs to -- summing fractions across
// pools would compare fractions of different denominators, so this caps on
// actual dollars against each pool's own dollar total instead.
const PORTFOLIO_CEILING_PCT = 0.6;

function recommendedKey(marketType: string, source: string, team: string | null, label: string): string {
  return `${marketType}|${source}|${team ?? label}`;
}

/** Non-null when a genuinely unresolved game-time decision exists for this
 * row's game -- see RecommendedBetRow.waitReason's docstring. `news_requires_review`
 * is computed backend-side per sport (injury_rules*.py) from statuses that
 * are still a real game-time decision (Questionable/Doubtful/Day-To-Day),
 * NOT already-settled ones (Out/IR/Suspension are fully priced into the
 * model already, nothing to wait for there). */
function computeWaitReason(m: MarketRow | NbaMarketRow | MlbMarketRow): string | null {
  if (m.news_requires_review) {
    return "A key player's status is a game-time decision, not yet confirmed -- the model's number may move once it is.";
  }
  return null;
}

/** Market types whose model_prob actually consumes the probable-pitcher
 * blend (see mlb_markets.py's _moneyline_model_prob/_spread_model_prob/
 * _f5_model_prob/_rfi_model_prob, all of which take probable_pitcher_id
 * params) -- total/team_total deliberately do NOT (their model is park
 * factor + team scoring rate, checked and confirmed pitcher ERA barely
 * moves it), so flagging a TBD pitcher there would be a misleading reason
 * for a number that isn't even affected by it. */
const MLB_PITCHER_DEPENDENT_MARKET_TYPES = new Set(["moneyline", "spread", "f5", "rfi"]);

/** MLB-specific: the starting-pitcher blend is this app's own documented
 * DOMINANT per-game signal (see elo_service_mlb.py) -- a game whose
 * starter(s) aren't officially announced yet is scored on team-Elo alone,
 * a real, meaningfully less-informed number than what it'll become once
 * MLB Stats API publishes the probable pitcher (usually 1-5 days out). */
function computeMlbWaitReason(m: MlbMarketRow): string | null {
  if (MLB_PITCHER_DEPENDENT_MARKET_TYPES.has(m.market_type) && (!m.home_probable_pitcher || !m.away_probable_pitcher)) {
    return "Starting pitcher not yet officially announced for one or both teams -- the model is using team strength alone until then.";
  }
  return computeWaitReason(m);
}

/** Same real-world proposition regardless of platform or ladder rung --
 * used to collapse Kalshi vs Polymarket duplicates (e.g. "AFC West Division
 * Winner" on Kalshi and "Pro Football: AFC West Champion" on Polymarket are
 * the same bet on the same team) and, combined with the ladder collapse
 * above, ladder rungs across sources too. */
/** Display-only label for the "weekly" stake_pool bucket (backend enum
 * value is unchanged -- "weekly"/"futures" -- see staking.py's
 * WEEKLY_MARKET_TYPES docstring: it's a FIXED sub-allocation freed as
 * pending bets settle, not a calendar-reset budget, so the underlying
 * mechanics are already sport-agnostic and correct). "Weekly" is
 * literally true for NFL (games really are ~weekly) but misleading for
 * NBA/MLB (games are near-daily/daily) and MMA (per-fight, sporadic
 * cards) -- caught via user feedback 2026-07-18. Renaming the stored
 * enum would need a data migration across every historical PlacedBet row
 * for zero functional gain, so this only changes what's SHOWN. */
export function perGamePoolLabel(sport: "nfl" | "nba" | "wnba" | "mlb" | "mma" | "tennis" | "soccer" | "valorant" | "cs2" | "lol"): string {
  if (sport === "nba" || sport === "wnba" || sport === "mlb") return "Daily";
  if (sport === "mma") return "Per-fight";
  if (sport === "tennis" || sport === "soccer" || sport === "valorant" || sport === "cs2" || sport === "lol") return "Per-match";
  return "Weekly";
}

export function crossPlatformKey(row: {
  marketType: string;
  sport?: string;
  nflGameId: string | null;
  nbaGameId?: string | null;
  wnbaGameId?: string | null;
  mlbGameId?: string | null;
  mmaFightId?: string | null;
  tennisMatchId?: number | null;
  soccerMatchId?: number | null;
  valorantMatchId?: number | null;
  cs2MatchId?: number | null;
  lolMatchId?: number | null;
  team: string | null;
  line: number | null;
  side: string | null;
  label: string;
}): string {
  // REAL BUG caught while building the NBA recommended-bets path
  // (2026-07-17): the NFL-only version below assumed a truthy nflGameId
  // means "this is a game-tied row" and anything else means "this is a
  // team-only futures row" -- correct for NFL (every game-tied candidate
  // always carries a real nfl_game_id), but NBA candidates were being built
  // with nflGameId always null (NbaMarketRow has no such field), so EVERY
  // NBA row -- including moneyline/spread/total, which really are tied to
  // one specific game -- fell into the team-only branch. That would have
  // silently collapsed the same team's DIFFERENT games together (e.g. two
  // separate BOS games both keying to "moneyline|BOS"). Checking nbaGameId
  // (and now mlbGameId, same reasoning) as a second real per-game
  // identifier fixes this without disturbing the NFL branch at all.
  //
  // SECOND REAL BUG caught while building MLB's RFI market (2026-07-17):
  // `side` was missing from this key entirely. RFI's "Yes"/"No" rows both
  // have team=null AND line=null (nothing else distinguishes them), so
  // without `side` they'd collide into ONE key every single time --
  // whichever side happened to have the better edge would silently discard
  // the other, opposite, real bet. The same gap could also bite
  // total/team_total whenever the best-edge Over and Under rung happen to
  // share the same line (a normal, common case for a paired market) --
  // latent for those since it depends on line coincidence, but guaranteed
  // for RFI. Including `side` fixes both without changing behavior for any
  // market type where the two sides never share a key otherwise.
  // REAL collision risk unique to esports (2026-07-19): valorantMatchId/
  // cs2MatchId/lolMatchId are all plain auto-increment integer primary keys
  // from THREE SEPARATE DB tables, unlike every other sport's game id here
  // (nflGameId/mmaFightId are naturally sport-scoped strings; tennisMatchId/
  // soccerMatchId are bare ints too, but never had this risk since each
  // sport builds its own separate candidate list). Each esports title also
  // builds its own separate list now (2026-07-20, once each got its own
  // bankroll pool -- see buildValorantRecommendedBets/
  // buildCs2RecommendedBets/buildLolRecommendedBets), so this prefix is no
  // longer load-bearing the way it was when all 3 titles' rows briefly
  // shared one combined candidate list -- left in place as a harmless,
  // still-correct defensive habit rather than ripped out.
  const gameId =
    row.nflGameId || row.nbaGameId || row.wnbaGameId || row.mlbGameId || row.mmaFightId || row.tennisMatchId || row.soccerMatchId ||
    (row.valorantMatchId ? `valorant:${row.valorantMatchId}` : null) ||
    (row.cs2MatchId ? `cs2:${row.cs2MatchId}` : null) ||
    (row.lolMatchId ? `lol:${row.lolMatchId}` : null);
  if (gameId) return `${gameId}|${row.marketType}|${row.team ?? ""}|${row.line ?? ""}|${row.side ?? ""}`;
  // `sport` scopes the FUTURES fallback too (real risk unique to esports:
  // the 3 titles reuse the same market_type string, e.g. "tournament_winner",
  // and Kalshi/Polymarket futures catalogs use generic placeholder team
  // names like "Team A".."Team S" for unannounced roster slots, confirmed
  // live for LoL's own LCK futures -- a coincidental name match across two
  // DIFFERENT titles' futures would otherwise silently collapse them
  // together). No-op for every existing single-sport builder (every row in
  // one of those calls already shares the same sport value).
  return `${row.sport ?? ""}|${row.marketType}|${row.team ?? row.label}`;
}

/** Real bug this fixes (caught live 2026-07-19, flagged during the same
 * investigation that found Polymarket volume was never being ingested at
 * all): every cross-platform collapse below picked whichever side showed
 * the bigger nominal `edge`, with no regard for how much real trading
 * backed that price -- a thin, barely-traded quote can show an arbitrarily
 * large "edge" against the model with no real market consensus behind it.
 * Validated against real live data, not assumed: MLB's "DET @ LAA Over 9.5"
 * had Polymarket at volume=1839/edge=0.1105 vs Kalshi at volume=390/
 * edge=0.1205 -- the old edge-only rule picked Kalshi despite Polymarket
 * carrying 4.7x the real volume. Volume is now the deciding factor (a
 * heavily-traded price is more trustworthy than a thin one showing a
 * naively bigger edge); edge only breaks an exact volume tie. */
function preferForCrossPlatformCollapse(candidate: RecommendedBetRow, existing: RecommendedBetRow): boolean {
  const candidateVolume = candidate.volume ?? 0;
  const existingVolume = existing.volume ?? 0;
  if (candidateVolume !== existingVolume) {
    return candidateVolume > existingVolume;
  }
  return (candidate.edge ?? 0) > (existing.edge ?? 0);
}

/** Real correlation-risk finding (2026-07-19, live data, same investigation
 * that led to preferForCrossPlatformCollapse above): moneyline/spread/total/
 * team_total for the SAME game are mostly downstream of the same
 * team-strength/scoring-environment assessment, not independent edges --
 * confirmed live, MLB's "NYM @ PHI" showed 3 separate 5%-of-pool positions
 * at once (moneyline PHI, spread PHI -2, and Polymarket's spread PHI +2)
 * that are really one "PHI is undervalued" belief stacked three ways: if
 * that belief is wrong, all three lose together, not three independent
 * risks -- which quietly undermines the whole point of sizing each position
 * at a conservative quarter-Kelly in the first place (that math assumes
 * each position is its own independent bet).
 *
 * Same reasoning AND same hard-cap-to-single-best-row shape as the existing
 * per-PLAYER cap below (a player's edge across several stat categories is
 * one signal too, not several) -- deliberately does NOT need its own
 * market-type allowlist the way that cap does: every row with a real game
 * id (nflGameId/nbaGameId/mlbGameId/mmaFightId/tennisMatchId) is, BY
 * DEFINITION, tied to one specific real-world game/fight/match, so capping
 * on game id alone is exactly as safe as capping the player-stat types on
 * player alone. Futures/season-long rows (no real game id -- confirmed live,
 * player-stat props are season-long, not game-tied) pass through untouched,
 * same as the player cap already leaves team-level futures alone (division
 * winner AND conference champion for the same team ARE genuinely separate
 * real propositions worth separate exposure). */
function capToOneRowPerGame(rows: RecommendedBetRow[]): RecommendedBetRow[] {
  const gameBest = new Map<string, RecommendedBetRow>();
  const nonGameRows: RecommendedBetRow[] = [];
  for (const row of rows) {
    // Same per-title-prefixed scoping as crossPlatformKey above, for the
    // same real cross-title id-collision reason.
    const gameId =
      row.nflGameId || row.nbaGameId || row.mlbGameId || row.mmaFightId || row.tennisMatchId || row.soccerMatchId ||
      (row.valorantMatchId ? `valorant:${row.valorantMatchId}` : null) ||
      (row.cs2MatchId ? `cs2:${row.cs2MatchId}` : null) ||
      (row.lolMatchId ? `lol:${row.lolMatchId}` : null);
    if (!gameId) {
      nonGameRows.push(row);
      continue;
    }
    const key = String(gameId);
    const existing = gameBest.get(key);
    if (!existing || (row.edge ?? 0) > (existing.edge ?? 0)) {
      gameBest.set(key, row);
    }
  }
  return [...nonGameRows, ...gameBest.values()].sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0));
}

/** Pools every market/futures row that cleared the Kelly staking threshold,
 * then fixes the two structural problems that made the raw pool nearly
 * useless in practice (2026-07-16 user feedback: "over 700 recommended
 * bets... some identical?"):
 *   1. Ladder rungs of the same underlying bet (LADDER_MARKET_TYPES) are
 *      collapsed to their single best-edge rung, per source.
 *   2. The same real-world proposition priced on both Kalshi and Polymarket
 *      is collapsed to whichever platform has the better edge.
 * The result is still sorted/ranked by edge, then capped SEPARATELY per
 * pool (weekly / futures -- see backend app/models/staking.py) at
 * PORTFOLIO_CEILING_PCT of that pool's own dollar amount, so the list can't
 * recommend committing more than a sane simultaneous-exposure ceiling of
 * either pool -- raising MIN_EDGE_TO_BET alone does NOT fix this (checked
 * against live data: even an 8pp gate still left 490/2192 futures rows --
 * the volume was never mostly noise, it's structural). */
export function buildRecommendedBets(
  markets: MarketRow[],
  futures: FuturesMarketRow[],
  weeklyPoolDollars: number,
  futuresPoolDollars: number,
  lockedWeeklyDollars = 0,
  lockedFuturesDollars = 0
): RecommendedBetsResult {
  const candidates: RecommendedBetRow[] = [];

  for (const m of markets) {
    if (m.kelly_fraction === null || m.suggested_stake_dollars === null || m.stake_pool === null) continue;
    const label = m.game_label ?? m.market_type;
    candidates.push({
      key: `market-${m.id}`,
      marketId: m.id,
      label,
      marketType: m.market_type,
      team: m.team,
      line: m.line,
      side: m.side,
      gameday: m.gameday,
      gametime: m.gametime,
      estimatedStartTime: null,
      source: m.source,
      impliedProb: m.implied_prob,
      estProb: m.final_prob ?? m.model_prob,
      edge: m.edge,
      lineMovePp: m.line_move_pp,
      kellyFraction: m.kelly_fraction,
      suggestedStakeDollars: m.suggested_stake_dollars,
      suggestedStakeUnits: m.suggested_stake_units,
      stakePool: m.stake_pool,
      volume: m.volume,
      nflGameId: m.nfl_game_id,
      sport: "nfl",
      nbaGameId: null,
      mlbGameId: null,
      mmaFightId: null,
      tennisMatchId: null,
      soccerMatchId: null,
      valorantMatchId: null,
      cs2MatchId: null,
      lolMatchId: null,
      groupKey: recommendedKey(m.market_type, m.source, m.team, label),
      waitReason: computeWaitReason(m),
    });
  }

  for (const f of futures) {
    if (f.kelly_fraction === null || f.suggested_stake_dollars === null || f.stake_pool === null) continue;
    const label = f.group_label ?? f.market_type;
    candidates.push({
      key: `futures-${f.id}`,
      marketId: f.id,
      label,
      marketType: f.market_type,
      team: f.team,
      line: f.line,
      side: f.side,
      gameday: null,
      gametime: null,
      estimatedStartTime: null,
      source: f.source,
      impliedProb: f.implied_prob,
      estProb: f.model_prob,
      edge: f.edge,
      lineMovePp: f.line_move_pp,
      kellyFraction: f.kelly_fraction,
      suggestedStakeDollars: f.suggested_stake_dollars,
      suggestedStakeUnits: f.suggested_stake_units,
      stakePool: f.stake_pool,
      volume: f.volume,
      nflGameId: null,
      sport: "nfl",
      nbaGameId: null,
      mlbGameId: null,
      mmaFightId: null,
      tennisMatchId: null,
      soccerMatchId: null,
      valorantMatchId: null,
      cs2MatchId: null,
      lolMatchId: null,
      groupKey: recommendedKey(f.market_type, f.source, f.team, label),
      waitReason: null, // season-long/futures props don't have a game-day "confirm the lineup" moment the same way
    });
  }

  const rawCandidateCount = candidates.length;

  // Pass 1: collapse ladder rungs within the same source (season-long
  // ladders like win_total, AND game-tied ladders like spread/total/
  // team_total -- see ladderCollapseKey's docstring).
  const ladderCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of candidates) {
    const key = ladderCollapseKey(row);
    const existing = ladderCollapsed.get(key);
    if (!existing || (row.edge ?? 0) > (existing.edge ?? 0)) {
      ladderCollapsed.set(key, row);
    }
  }

  // Pass 2: collapse the same real-world proposition across Kalshi/Polymarket.
  const crossPlatformCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of ladderCollapsed.values()) {
    const key = crossPlatformKey(row);
    const existing = crossPlatformCollapsed.get(key);
    if (!existing || preferForCrossPlatformCollapse(row, existing)) {
      crossPlatformCollapsed.set(key, row);
    }
  }

  const deduped = Array.from(crossPlatformCollapsed.values()).sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0));

  // Pass 3: a single PLAYER generating edge across multiple stat categories
  // (e.g. Christian McCaffrey showing up on both season_rec_yds AND
  // season_rec) is really one underlying signal -- more volume/health for
  // that player -- not several independent opportunities, unlike a team
  // appearing in multiple genuinely-different futures (division winner AND
  // conference champion are real, distinct propositions worth separate
  // exposure, so this cap deliberately does NOT apply to team-level
  // futures). Found live 2026-07-16 while reviewing the sizing rework --
  // caps to the single best-edge stat line per player.
  const playerBest = new Map<string, RecommendedBetRow>();
  const nonPlayerRows: RecommendedBetRow[] = [];
  for (const row of deduped) {
    if (!PLAYER_STAT_MARKET_TYPES.has(row.marketType)) {
      nonPlayerRows.push(row);
      continue;
    }
    const playerKey = row.team ?? row.label;
    const existing = playerBest.get(playerKey);
    if (!existing || (row.edge ?? 0) > (existing.edge ?? 0)) {
      playerBest.set(playerKey, row);
    }
  }
  const afterEntityCap = [...nonPlayerRows, ...playerBest.values()].sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0));

  // Pass 3b: one row per real-world GAME, same reasoning/shape as the
  // per-player cap just above -- see capToOneRowPerGame's own docstring.
  const afterGameCap = capToOneRowPerGame(afterEntityCap);

  // Pass 4: portfolio cap -- stop including a pool's rows once its
  // cumulative dollar total would exceed PORTFOLIO_CEILING_PCT of that
  // pool's own dollar amount. Tracked separately per pool since weekly and
  // futures draw from different sub-allocations (see Settings). Capital
  // already committed to a PENDING placed bet (lockedWeeklyDollars/
  // lockedFuturesDollars) counts against the ceiling too -- otherwise
  // marking a bet as placed wouldn't actually reduce room for new
  // recommendations, defeating the point of tracking it.
  const poolCeilings = {
    weekly: Math.max(0, weeklyPoolDollars * PORTFOLIO_CEILING_PCT - lockedWeeklyDollars),
    futures: Math.max(0, futuresPoolDollars * PORTFOLIO_CEILING_PCT - lockedFuturesDollars),
  };
  const cumulative = { weekly: 0, futures: 0 };
  const shown: RecommendedBetRow[] = [];
  for (const row of afterGameCap) {
    if (cumulative[row.stakePool] + row.suggestedStakeDollars > poolCeilings[row.stakePool]) continue;
    cumulative[row.stakePool] += row.suggestedStakeDollars;
    shown.push(row);
  }

  return {
    rows: shown,
    rawCandidateCount,
    collapsedCount: rawCandidateCount - afterGameCap.length,
    cutByPortfolioCapCount: afterGameCap.length - shown.length,
  };
}

/** NBA equivalent of buildRecommendedBets, deliberately leaner: NBA has no
 * ladder markets (win_total/season-stat thresholds) or player-stat markets
 * yet, so Pass 1 (ladder collapse) and Pass 3 (player-stat cap) from the
 * NFL version don't apply -- only cross-platform collapse (Pass 2) and the
 * portfolio cap (Pass 4) are needed. Kept as a separate function rather
 * than parameterizing buildRecommendedBets, since NbaMarketRow/NbaFutures
 * use `nba_game_id` not `nfl_game_id` and forcing them through the same
 * function would need an awkward field-name shim either way. */
export function buildNbaRecommendedBets(
  markets: NbaMarketRow[],
  futures: FuturesMarketRow[],
  weeklyPoolDollars: number,
  futuresPoolDollars: number,
  lockedWeeklyDollars = 0,
  lockedFuturesDollars = 0
): RecommendedBetsResult {
  const candidates: RecommendedBetRow[] = [];

  for (const m of markets) {
    if (m.kelly_fraction === null || m.suggested_stake_dollars === null || m.stake_pool === null) continue;
    const label = m.game_label ?? m.market_type;
    candidates.push({
      key: `market-${m.id}`,
      marketId: m.id,
      label,
      marketType: m.market_type,
      team: m.team,
      line: m.line,
      side: m.side,
      gameday: m.gameday,
      gametime: m.gametime,
      estimatedStartTime: null,
      source: m.source,
      impliedProb: m.implied_prob,
      estProb: m.final_prob ?? m.model_prob,
      edge: m.edge,
      lineMovePp: null,
      kellyFraction: m.kelly_fraction,
      suggestedStakeDollars: m.suggested_stake_dollars,
      suggestedStakeUnits: m.suggested_stake_units,
      stakePool: m.stake_pool,
      volume: m.volume,
      nflGameId: null,
      sport: "nba",
      nbaGameId: m.nba_game_id, // real per-game id -- see crossPlatformKey's docstring for why this matters for NBA specifically
      mlbGameId: null,
      mmaFightId: null,
      tennisMatchId: null,
      soccerMatchId: null,
      valorantMatchId: null,
      cs2MatchId: null,
      lolMatchId: null,
      groupKey: recommendedKey(m.market_type, m.source, m.team, label),
      waitReason: computeWaitReason(m),
    });
  }

  for (const f of futures) {
    if (f.kelly_fraction === null || f.suggested_stake_dollars === null || f.stake_pool === null) continue;
    const label = f.group_label ?? f.market_type;
    candidates.push({
      key: `futures-${f.id}`,
      marketId: f.id,
      label,
      marketType: f.market_type,
      team: f.team,
      line: f.line,
      side: f.side,
      gameday: null,
      gametime: null,
      estimatedStartTime: null,
      source: f.source,
      impliedProb: f.implied_prob,
      estProb: f.model_prob,
      edge: f.edge,
      lineMovePp: f.line_move_pp,
      kellyFraction: f.kelly_fraction,
      suggestedStakeDollars: f.suggested_stake_dollars,
      suggestedStakeUnits: f.suggested_stake_units,
      stakePool: f.stake_pool,
      volume: f.volume,
      nflGameId: null,
      sport: "nba",
      nbaGameId: null,
      mlbGameId: null,
      mmaFightId: null,
      tennisMatchId: null,
      soccerMatchId: null,
      valorantMatchId: null,
      cs2MatchId: null,
      lolMatchId: null,
      groupKey: recommendedKey(f.market_type, f.source, f.team, label),
      waitReason: null, // season-long/futures props don't have a game-day "confirm the lineup" moment the same way
    });
  }

  const rawCandidateCount = candidates.length;

  // Pass 1: collapse game-tied ladder rungs (spread/total/team_total) within
  // the same source -- added 2026-07-17 after a real user report (MLB team-
  // total showed "Over 3.5"/"Over 4.5"/"Over 5.5" for the same game all at
  // once). NBA has no season-long LADDER_MARKET_TYPES yet, only the
  // game-tied kind, but ladderCollapseKey handles both correctly either way.
  const ladderCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of candidates) {
    const key = ladderCollapseKey(row);
    const existing = ladderCollapsed.get(key);
    if (!existing || (row.edge ?? 0) > (existing.edge ?? 0)) {
      ladderCollapsed.set(key, row);
    }
  }

  // Pass 2: cross-platform collapse (see buildRecommendedBets Pass 2) --
  // crossPlatformKey falls back to its `market_type|team-or-label` branch
  // for every NBA row since nflGameId is always null here, which is
  // exactly the key shape futures rows already use, so this degrades
  // correctly rather than needing its own key function.
  const crossPlatformCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of ladderCollapsed.values()) {
    const key = crossPlatformKey(row);
    const existing = crossPlatformCollapsed.get(key);
    if (!existing || preferForCrossPlatformCollapse(row, existing)) {
      crossPlatformCollapsed.set(key, row);
    }
  }
  const deduped = Array.from(crossPlatformCollapsed.values()).sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0));

  // Pass 3: one row per real-world GAME (see capToOneRowPerGame's docstring).
  const gameCapped = capToOneRowPerGame(deduped);

  const poolCeilings = {
    weekly: Math.max(0, weeklyPoolDollars * PORTFOLIO_CEILING_PCT - lockedWeeklyDollars),
    futures: Math.max(0, futuresPoolDollars * PORTFOLIO_CEILING_PCT - lockedFuturesDollars),
  };
  const cumulative = { weekly: 0, futures: 0 };
  const shown: RecommendedBetRow[] = [];
  for (const row of gameCapped) {
    if (cumulative[row.stakePool] + row.suggestedStakeDollars > poolCeilings[row.stakePool]) continue;
    cumulative[row.stakePool] += row.suggestedStakeDollars;
    shown.push(row);
  }

  return {
    rows: shown,
    rawCandidateCount,
    collapsedCount: rawCandidateCount - gameCapped.length,
    cutByPortfolioCapCount: gameCapped.length - shown.length,
  };
}

/** WNBA equivalent of buildNbaRecommendedBets, moneyline-only (no futures,
 * no ladder, no news layer -- see wnba_markets.py). Same collapse/game-cap/
 * portfolio-cap passes; sets sport:"wnba" and wnbaGameId so crossPlatformKey
 * and markBetPlaced route to the right game-id field. */
export function buildWnbaRecommendedBets(
  markets: WnbaMarketRow[],
  weeklyPoolDollars: number,
  lockedWeeklyDollars = 0
): RecommendedBetsResult {
  const candidates: RecommendedBetRow[] = [];
  for (const m of markets) {
    if (m.kelly_fraction === null || m.suggested_stake_dollars === null || m.stake_pool === null) continue;
    const label = m.game_label ?? m.market_type;
    candidates.push({
      key: `market-${m.id}`,
      marketId: m.id,
      label,
      marketType: m.market_type,
      team: m.team,
      line: null,
      side: null,
      gameday: m.gameday,
      gametime: m.gametime,
      estimatedStartTime: null,
      source: m.source,
      impliedProb: m.implied_prob,
      estProb: m.model_prob,
      edge: m.edge,
      lineMovePp: null,
      kellyFraction: m.kelly_fraction,
      suggestedStakeDollars: m.suggested_stake_dollars,
      suggestedStakeUnits: m.suggested_stake_units,
      stakePool: m.stake_pool,
      volume: m.volume,
      nflGameId: null,
      sport: "wnba",
      nbaGameId: null,
      wnbaGameId: m.wnba_game_id,
      mlbGameId: null,
      mmaFightId: null,
      tennisMatchId: null,
      soccerMatchId: null,
      valorantMatchId: null,
      cs2MatchId: null,
      lolMatchId: null,
      groupKey: recommendedKey(m.market_type, m.source, m.team, label),
      waitReason: null, // WNBA has no news/injury layer wired yet
    });
  }

  const rawCandidateCount = candidates.length;

  const crossPlatformCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of candidates) {
    const key = crossPlatformKey(row);
    const existing = crossPlatformCollapsed.get(key);
    if (!existing || preferForCrossPlatformCollapse(row, existing)) {
      crossPlatformCollapsed.set(key, row);
    }
  }
  const deduped = Array.from(crossPlatformCollapsed.values()).sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0));
  const gameCapped = capToOneRowPerGame(deduped);

  const weeklyCeiling = Math.max(0, weeklyPoolDollars * PORTFOLIO_CEILING_PCT - lockedWeeklyDollars);
  let cumulative = 0;
  const shown: RecommendedBetRow[] = [];
  for (const row of gameCapped) {
    if (cumulative + row.suggestedStakeDollars > weeklyCeiling) continue;
    cumulative += row.suggestedStakeDollars;
    shown.push(row);
  }

  return {
    rows: shown,
    rawCandidateCount,
    collapsedCount: rawCandidateCount - gameCapped.length,
    cutByPortfolioCapCount: gameCapped.length - shown.length,
  };
}

/** MLB equivalent of buildNbaRecommendedBets -- same leaner shape (no
 * ladder or player-stat markets yet), cross-platform collapse + portfolio
 * cap only. Kept separate rather than parameterizing, same "different
 * game-id field name" reasoning as buildNbaRecommendedBets. */
export function buildMlbRecommendedBets(
  markets: MlbMarketRow[],
  futures: FuturesMarketRow[],
  weeklyPoolDollars: number,
  futuresPoolDollars: number,
  lockedWeeklyDollars = 0,
  lockedFuturesDollars = 0
): RecommendedBetsResult {
  const candidates: RecommendedBetRow[] = [];

  for (const m of markets) {
    if (m.kelly_fraction === null || m.suggested_stake_dollars === null || m.stake_pool === null) continue;
    const label = m.game_label ?? m.market_type;
    candidates.push({
      key: `market-${m.id}`,
      marketId: m.id,
      label,
      marketType: m.market_type,
      team: m.team,
      line: m.line,
      side: m.side,
      gameday: m.gameday,
      gametime: m.gametime,
      estimatedStartTime: null,
      source: m.source,
      impliedProb: m.implied_prob,
      estProb: m.final_prob ?? m.model_prob,
      edge: m.edge,
      lineMovePp: null,
      kellyFraction: m.kelly_fraction,
      suggestedStakeDollars: m.suggested_stake_dollars,
      suggestedStakeUnits: m.suggested_stake_units,
      stakePool: m.stake_pool,
      volume: m.volume,
      nflGameId: null,
      sport: "mlb",
      nbaGameId: null,
      mlbGameId: m.mlb_game_id, // real per-game id -- see crossPlatformKey's docstring
      mmaFightId: null,
      tennisMatchId: null,
      soccerMatchId: null,
      valorantMatchId: null,
      cs2MatchId: null,
      lolMatchId: null,
      groupKey: recommendedKey(m.market_type, m.source, m.team, label),
      waitReason: computeMlbWaitReason(m),
    });
  }

  for (const f of futures) {
    if (f.kelly_fraction === null || f.suggested_stake_dollars === null || f.stake_pool === null) continue;
    const label = f.group_label ?? f.market_type;
    candidates.push({
      key: `futures-${f.id}`,
      marketId: f.id,
      label,
      marketType: f.market_type,
      team: f.team,
      line: f.line,
      side: f.side,
      gameday: null,
      gametime: null,
      estimatedStartTime: null,
      source: f.source,
      impliedProb: f.implied_prob,
      estProb: f.model_prob,
      edge: f.edge,
      lineMovePp: f.line_move_pp,
      kellyFraction: f.kelly_fraction,
      suggestedStakeDollars: f.suggested_stake_dollars,
      suggestedStakeUnits: f.suggested_stake_units,
      stakePool: f.stake_pool,
      volume: f.volume,
      nflGameId: null,
      sport: "mlb",
      nbaGameId: null,
      mlbGameId: null,
      mmaFightId: null,
      tennisMatchId: null,
      soccerMatchId: null,
      valorantMatchId: null,
      cs2MatchId: null,
      lolMatchId: null,
      groupKey: recommendedKey(f.market_type, f.source, f.team, label),
      waitReason: null, // season-long/futures props don't have a game-day "confirm the lineup" moment the same way
    });
  }

  const rawCandidateCount = candidates.length;

  // Pass 1: collapse game-tied ladder rungs (spread/total/team_total) within
  // the same source -- this is the exact bug a user reported live
  // 2026-07-17 (MLB team-total showed "Over 3.5"/"Over 4.5"/"Over 5.5" for
  // the same game all at once, since neither was collapsed before this fix).
  const ladderCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of candidates) {
    const key = ladderCollapseKey(row);
    const existing = ladderCollapsed.get(key);
    if (!existing || (row.edge ?? 0) > (existing.edge ?? 0)) {
      ladderCollapsed.set(key, row);
    }
  }

  // Pass 2: cross-platform collapse (see buildRecommendedBets Pass 2).
  const crossPlatformCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of ladderCollapsed.values()) {
    const key = crossPlatformKey(row);
    const existing = crossPlatformCollapsed.get(key);
    if (!existing || preferForCrossPlatformCollapse(row, existing)) {
      crossPlatformCollapsed.set(key, row);
    }
  }
  const deduped = Array.from(crossPlatformCollapsed.values()).sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0));

  // Pass 3: one row per real-world GAME (see capToOneRowPerGame's docstring).
  const gameCapped = capToOneRowPerGame(deduped);

  const poolCeilings = {
    weekly: Math.max(0, weeklyPoolDollars * PORTFOLIO_CEILING_PCT - lockedWeeklyDollars),
    futures: Math.max(0, futuresPoolDollars * PORTFOLIO_CEILING_PCT - lockedFuturesDollars),
  };
  const cumulative = { weekly: 0, futures: 0 };
  const shown: RecommendedBetRow[] = [];
  for (const row of gameCapped) {
    if (cumulative[row.stakePool] + row.suggestedStakeDollars > poolCeilings[row.stakePool]) continue;
    cumulative[row.stakePool] += row.suggestedStakeDollars;
    shown.push(row);
  }

  return {
    rows: shown,
    rawCandidateCount,
    collapsedCount: rawCandidateCount - gameCapped.length,
    cutByPortfolioCapCount: gameCapped.length - shown.length,
  };
}

/** MMA equivalent of buildMlbRecommendedBets. No futures param yet --
 * MMA futures (KXUFCTITLE family) are deliberately built last/low-priority
 * (thin/illiquid compared to NFL's season futures, and a single UFC card
 * already generates ~70+ per-fight markets across 6 market types at once,
 * so nearly all the real opportunity here is per-fight). Moneyline,
 * distance, method_of_finish, and rounds all have real models now;
 * method_of_victory/round_of_victory still ship model_prob=null, so they
 * never clear the kelly_fraction!==null filter below and can't appear
 * here yet -- nothing to filter out specially for that). */
export function buildMmaRecommendedBets(
  markets: MmaMarketRow[],
  weeklyPoolDollars: number,
  lockedWeeklyDollars = 0
): RecommendedBetsResult {
  const candidates: RecommendedBetRow[] = [];

  for (const m of markets) {
    if (m.kelly_fraction === null || m.suggested_stake_dollars === null || m.stake_pool === null) continue;
    const label = m.fight_label ?? m.market_type;
    candidates.push({
      key: `market-${m.id}`,
      marketId: m.id,
      label,
      marketType: m.market_type,
      team: m.team,
      line: m.line,
      side: m.side,
      gameday: m.event_date,
      gametime: null, // see estimatedStartTime below -- MMA's own real per-fight instant is used directly instead of a reconstructed gameday+gametime pair
      estimatedStartTime: m.estimated_start_time,
      source: m.source,
      impliedProb: m.implied_prob,
      estProb: m.model_prob, // no news/situational blend exists for MMA yet, so model_prob IS the final estimate (no final_prob field to prefer)
      edge: m.edge,
      lineMovePp: null,
      kellyFraction: m.kelly_fraction,
      suggestedStakeDollars: m.suggested_stake_dollars,
      suggestedStakeUnits: m.suggested_stake_units,
      stakePool: m.stake_pool,
      volume: m.volume,
      nflGameId: null,
      sport: "mma",
      nbaGameId: null,
      mlbGameId: null,
      mmaFightId: m.mma_fight_id,
      tennisMatchId: null,
      soccerMatchId: null,
      valorantMatchId: null,
      cs2MatchId: null,
      lolMatchId: null,
      groupKey: recommendedKey(m.market_type, m.source, m.team, label),
      waitReason: null, // no injury-report-style situational data source exists for MMA yet (checked, see project memory) -- nothing to flag
    });
  }

  const rawCandidateCount = candidates.length;

  // REAL BUG fixed here (2026-07-19, user report: seeing "ends before round
  // 5", "goes past round 4.5", AND "ends before round 3" all recommended at
  // once for the SAME fight): this comment used to claim no MMA market type
  // is a ladder, which was true when only moneyline/distance existed but
  // went stale once `rounds` (a real Over/Under N.5-rounds ladder) got a
  // model -- every rung that cleared the edge threshold showed up with
  // nothing collapsing them, on BOTH the over and under side. Same root
  // cause and same fix as gameLadderKey's own fix (see its docstring): an
  // Over 4.5 and an Under 2.5 pick on the same fight aren't two independent
  // opportunities, they're the model's one view of the fight's length read
  // against two different market lines -- recommending both isn't
  // diversification, and if the fight ends in exactly round 3 or 4 both
  // bets lose. Collapsed here to the single best-edge rung per fight,
  // across BOTH directions (side deliberately excluded from the key).
  const mmaLadderKey = (row: RecommendedBetRow) => `${row.marketType}|${row.source}|${row.mmaFightId}`;
  const ladderCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of candidates) {
    const key = row.marketType === "rounds" ? mmaLadderKey(row) : row.key;
    const existing = ladderCollapsed.get(key);
    if (!existing || (row.edge ?? 0) > (existing.edge ?? 0)) {
      ladderCollapsed.set(key, row);
    }
  }
  const ladderDeduped = Array.from(ladderCollapsed.values());

  // Cross-platform collapse: the same real-world proposition (same fight,
  // same market_type/team/side) on Kalshi vs Polymarket collapses to
  // whichever platform prices it better.
  const crossPlatformCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of ladderDeduped) {
    const key = crossPlatformKey(row);
    const existing = crossPlatformCollapsed.get(key);
    if (!existing || preferForCrossPlatformCollapse(row, existing)) {
      crossPlatformCollapsed.set(key, row);
    }
  }
  const deduped = Array.from(crossPlatformCollapsed.values()).sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0));

  // Pass 3: one row per real-world GAME (see capToOneRowPerGame's docstring).
  const gameCapped = capToOneRowPerGame(deduped);

  const weeklyCeiling = Math.max(0, weeklyPoolDollars * PORTFOLIO_CEILING_PCT - lockedWeeklyDollars);
  let cumulative = 0;
  const shown: RecommendedBetRow[] = [];
  for (const row of gameCapped) {
    if (cumulative + row.suggestedStakeDollars > weeklyCeiling) continue;
    cumulative += row.suggestedStakeDollars;
    shown.push(row);
  }

  return {
    rows: shown,
    rawCandidateCount,
    collapsedCount: rawCandidateCount - gameCapped.length,
    cutByPortfolioCapCount: gameCapped.length - shown.length,
  };
}

/** Tennis's own version of buildMmaRecommendedBets -- moneyline only in this
 * build (Phase 2 scope), no futures/situational blend, so the same
 * "cross-platform collapse only, no ladder types" shape applies. Every
 * moneyline market in this app is backtested NO-GO against the market (see
 * elo_tennis.py's docstring) -- this list is a mechanical filter on
 * unvalidated model output, not a claim of edge, same as every other sport. */
export function buildTennisRecommendedBets(
  markets: TennisMarketRow[],
  weeklyPoolDollars: number,
  lockedWeeklyDollars = 0
): RecommendedBetsResult {
  const candidates: RecommendedBetRow[] = [];

  for (const m of markets) {
    if (m.kelly_fraction === null || m.suggested_stake_dollars === null || m.stake_pool === null) continue;
    const label = m.match_label ?? m.market_type;
    candidates.push({
      key: `market-${m.id}`,
      marketId: m.id,
      label,
      marketType: m.market_type,
      team: m.team,
      line: m.line,
      side: m.side,
      gameday: m.match_date,
      gametime: null,
      estimatedStartTime: m.estimated_start_time,
      source: m.source,
      impliedProb: m.implied_prob,
      estProb: m.model_prob,
      edge: m.edge,
      lineMovePp: null,
      kellyFraction: m.kelly_fraction,
      suggestedStakeDollars: m.suggested_stake_dollars,
      suggestedStakeUnits: m.suggested_stake_units,
      stakePool: m.stake_pool,
      volume: m.volume,
      nflGameId: null,
      sport: "tennis",
      nbaGameId: null,
      mlbGameId: null,
      mmaFightId: null,
      tennisMatchId: m.tennis_match_id,
      soccerMatchId: null,
      valorantMatchId: null,
      cs2MatchId: null,
      lolMatchId: null,
      groupKey: recommendedKey(m.market_type, m.source, m.team, label),
      waitReason: null, // no injury-report-style situational data source exists for Tennis yet
    });
  }

  const rawCandidateCount = candidates.length;

  // set_winner/game_spread/game_total/set_total/total_sets are real ladders
  // (multiple sets/lines per match, same underlying question at different
  // rungs) -- collapse to the single best-edge rung per (market_type,
  // source, tennisMatchId, team/side), same reasoning as
  // GAME_LADDER_MARKET_TYPES elsewhere in this file. exact_score is NOT a
  // ladder (each scoreline is a genuinely distinct proposition, not the same
  // question at a different threshold), so it's deliberately excluded and
  // falls through to row.key as-is.
  //
  // REAL BUG fixed here (2026-07-19, same root cause as gameLadderKey's own
  // fix -- see its docstring): `side` used to be in this key unconditionally,
  // which let the best "over" rung AND the best "under" rung of
  // game_spread/game_total/total_sets both survive as separate
  // "opportunities" even though they're the same underlying number read two
  // ways, not independent bets. `set_total` is the one real exception --
  // its `side` field isn't a direction at all, it identifies WHICH SET the
  // line belongs to ("set_1"/"set_2"), a genuinely different real-world
  // proposition per set that must stay separate.
  const TENNIS_LADDER_TYPES = new Set(["set_winner", "game_spread", "game_total", "set_total", "total_sets"]);
  const tennisLadderKey = (row: RecommendedBetRow) => {
    const sideKey = row.marketType === "set_total" ? (row.side ?? "") : "";
    return `${row.marketType}|${row.source}|${row.tennisMatchId}|${row.team ?? ""}|${sideKey}`;
  };
  const ladderCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of candidates) {
    const key = TENNIS_LADDER_TYPES.has(row.marketType) ? tennisLadderKey(row) : row.key;
    const existing = ladderCollapsed.get(key);
    if (!existing || (row.edge ?? 0) > (existing.edge ?? 0)) {
      ladderCollapsed.set(key, row);
    }
  }
  const ladderDeduped = Array.from(ladderCollapsed.values());

  const crossPlatformCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of ladderDeduped) {
    const key = crossPlatformKey(row);
    const existing = crossPlatformCollapsed.get(key);
    if (!existing || preferForCrossPlatformCollapse(row, existing)) {
      crossPlatformCollapsed.set(key, row);
    }
  }
  const deduped = Array.from(crossPlatformCollapsed.values()).sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0));

  // Pass 3: one row per real-world GAME (see capToOneRowPerGame's docstring).
  const gameCapped = capToOneRowPerGame(deduped);

  const weeklyCeiling = Math.max(0, weeklyPoolDollars * PORTFOLIO_CEILING_PCT - lockedWeeklyDollars);
  let cumulative = 0;
  const shown: RecommendedBetRow[] = [];
  for (const row of gameCapped) {
    if (cumulative + row.suggestedStakeDollars > weeklyCeiling) continue;
    cumulative += row.suggestedStakeDollars;
    shown.push(row);
  }

  return {
    rows: shown,
    rawCandidateCount,
    collapsedCount: rawCandidateCount - gameCapped.length,
    cutByPortfolioCapCount: gameCapped.length - shown.length,
  };
}

/** Soccer's own version of buildTennisRecommendedBets. moneyline_3way needs
 * no ladder-collapse (each match produces exactly 3 real, distinct
 * propositions -- home/draw/away -- and `side` already makes each one its
 * own crossPlatformKey), but game_spread/game_total ARE real ladders (2
 * lines x 2 teams for spread, up to 6 lines for total, same shape as
 * Tennis's own game_spread/game_total) and need the same rung-collapse pass
 * Tennis's TENNIS_LADDER_TYPES does, or the best "1.5" rung and the best
 * "2.5" rung of the SAME underlying spread would both survive as separate
 * "opportunities" even though they're correlated bets on the same real
 * proposition. Every market type here (including Soccer's, see
 * elo_soccer.py's backtest results) is backtested NO-GO against the market
 * -- this list is a mechanical filter on unvalidated model output, not a
 * claim of edge, same as every other sport. */
export function buildSoccerRecommendedBets(
  markets: SoccerMarketRow[],
  weeklyPoolDollars: number,
  lockedWeeklyDollars = 0
): RecommendedBetsResult {
  const candidates: RecommendedBetRow[] = [];

  for (const m of markets) {
    if (m.kelly_fraction === null || m.suggested_stake_dollars === null || m.stake_pool === null) continue;
    const label = m.match_label ?? m.market_type;
    candidates.push({
      key: `market-${m.id}`,
      marketId: m.id,
      label,
      marketType: m.market_type,
      team: m.team,
      line: m.line,
      side: m.side,
      correctScoreHome: m.correct_score_home,
      correctScoreAway: m.correct_score_away,
      gameday: m.match_date,
      gametime: null,
      estimatedStartTime: m.estimated_start_time,
      source: m.source,
      impliedProb: m.implied_prob,
      estProb: m.model_prob,
      edge: m.edge,
      lineMovePp: null,
      kellyFraction: m.kelly_fraction,
      suggestedStakeDollars: m.suggested_stake_dollars,
      suggestedStakeUnits: m.suggested_stake_units,
      stakePool: m.stake_pool,
      volume: m.volume,
      nflGameId: null,
      sport: "soccer",
      nbaGameId: null,
      mlbGameId: null,
      mmaFightId: null,
      tennisMatchId: null,
      soccerMatchId: m.soccer_match_id,
      valorantMatchId: null,
      cs2MatchId: null,
      lolMatchId: null,
      groupKey: recommendedKey(m.market_type, m.source, m.team, label),
      waitReason: null, // no injury-report-style situational data source exists for Soccer yet
    });
  }

  const rawCandidateCount = candidates.length;

  // Pass 1: collapse spread/total's real ladder rungs (multiple lines per
  // team/match) to the single best-edge rung per (market_type, source,
  // soccerMatchId, team/side) -- same reasoning/shape as Tennis's own
  // TENNIS_LADDER_TYPES collapse. moneyline_3way is NOT in this set (no
  // ladder to collapse -- home/draw/away are 3 distinct propositions, not
  // rungs of the same one).
  // Second batch (added 2026-07-19): team_total and every first_half_*/
  // second_half_* spread/total/team_total variant are ALSO real threshold
  // ladders (multiple lines per team/match) -- correct_score is NOT (30
  // mutually-exclusive discrete scorelines, not threshold rungs, already
  // collapsed down to one row per real match by the per-game cap below
  // instead); ftts/first_half_winner/second_half_winner are also NOT (3
  // distinct propositions each, same reasoning as moneyline_3way).
  const SOCCER_LADDER_TYPES = new Set([
    "game_spread", "game_total", "team_total",
    "first_half_spread", "first_half_total", "first_half_team_total",
    "second_half_spread", "second_half_total", "second_half_team_total",
  ]);
  const soccerLadderKey = (row: RecommendedBetRow) =>
    `${row.marketType}|${row.source}|${row.soccerMatchId}|${row.team ?? ""}|${row.side ?? ""}`;
  const ladderCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of candidates) {
    const key = SOCCER_LADDER_TYPES.has(row.marketType) ? soccerLadderKey(row) : row.key;
    const existing = ladderCollapsed.get(key);
    if (!existing || (row.edge ?? 0) > (existing.edge ?? 0)) {
      ladderCollapsed.set(key, row);
    }
  }
  const ladderDeduped = Array.from(ladderCollapsed.values());

  const crossPlatformCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of ladderDeduped) {
    const key = crossPlatformKey(row);
    const existing = crossPlatformCollapsed.get(key);
    if (!existing || preferForCrossPlatformCollapse(row, existing)) {
      crossPlatformCollapsed.set(key, row);
    }
  }
  const deduped = Array.from(crossPlatformCollapsed.values()).sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0));

  // Pass 3: one row per real-world MATCH (see capToOneRowPerGame's docstring) --
  // otherwise a single match's home/draw/away rows (all 3 genuinely
  // correlated: only one can actually win), or its spread/total rows, would
  // each count as a separate "opportunity" toward the portfolio cap, same
  // stacking risk capToOneRowPerGame was built to fix for every other sport.
  const gameCapped = capToOneRowPerGame(deduped);

  const weeklyCeiling = Math.max(0, weeklyPoolDollars * PORTFOLIO_CEILING_PCT - lockedWeeklyDollars);
  let cumulative = 0;
  const shown: RecommendedBetRow[] = [];
  for (const row of gameCapped) {
    if (cumulative + row.suggestedStakeDollars > weeklyCeiling) continue;
    cumulative += row.suggestedStakeDollars;
    shown.push(row);
  }

  return {
    rows: shown,
    rawCandidateCount,
    collapsedCount: rawCandidateCount - gameCapped.length,
    cutByPortfolioCapCount: gameCapped.length - shown.length,
  };
}

/** Shared build logic behind buildValorantRecommendedBets/
 * buildCs2RecommendedBets/buildLolRecommendedBets below -- NOT exported,
 * just factored out to avoid 3 copies of the same dual-pool/ladder/
 * cross-platform/per-game-cap pipeline now that each title builds its own
 * single-title list (unlike the 3-title combine this replaced, see git
 * history/CLAUDE session notes for why that shared-pool design existed
 * only while esports had ONE combined bankroll allocation). Each esports
 * title's own market_type set is passed in as `ladderTypes` since it
 * differs slightly per title (e.g. series_handicap only exists for
 * Valorant) -- see each title's own GAME_MARKET_TYPES in its router. */
function buildEsportsTitleRecommendedBets<M extends { id: number; market_type: string; source: "kalshi" | "polymarket"; kelly_fraction: number | null; suggested_stake_dollars: number | null; suggested_stake_units: number | null; stake_pool: "weekly" | "futures" | null; team: string | null; line: number | null; side: string | null; match_date: string | null; estimated_start_time: string | null; implied_prob: number | null; model_prob: number | null; edge: number | null; volume: number | null; match_label?: string | null; group_label?: string | null }>(
  sport: "valorant" | "cs2" | "lol",
  markets: M[],
  matchIdOf: (m: M) => number | null,
  ladderTypes: Set<string>,
  weeklyPoolDollars: number,
  futuresPoolDollars: number,
  lockedWeeklyDollars: number,
  lockedFuturesDollars: number
): RecommendedBetsResult {
  const candidates: RecommendedBetRow[] = [];

  for (const m of markets) {
    if (m.kelly_fraction === null || m.suggested_stake_dollars === null || m.stake_pool === null) continue;
    const label = m.match_label ?? m.group_label ?? m.market_type;
    const matchId = matchIdOf(m);
    candidates.push({
      key: `market-${m.id}`,
      marketId: m.id,
      label,
      marketType: m.market_type,
      team: m.team,
      line: m.line,
      side: m.side,
      correctScoreHome: null,
      correctScoreAway: null,
      gameday: m.match_date,
      gametime: null,
      estimatedStartTime: m.estimated_start_time,
      source: m.source,
      impliedProb: m.implied_prob,
      estProb: m.model_prob,
      edge: m.edge,
      lineMovePp: null,
      kellyFraction: m.kelly_fraction,
      suggestedStakeDollars: m.suggested_stake_dollars,
      suggestedStakeUnits: m.suggested_stake_units,
      stakePool: m.stake_pool,
      volume: m.volume,
      nflGameId: null,
      sport,
      nbaGameId: null,
      mlbGameId: null,
      mmaFightId: null,
      tennisMatchId: null,
      soccerMatchId: null,
      valorantMatchId: sport === "valorant" ? matchId : null,
      cs2MatchId: sport === "cs2" ? matchId : null,
      lolMatchId: sport === "lol" ? matchId : null,
      groupKey: recommendedKey(m.market_type, m.source, m.team, label),
      // Esports have no "Wait" reason: the roster-change badge was retired
      // 2026-07-23 (no post-roster-change accuracy penalty -- see
      // calibrate_cs2_roster_window.py -- so nothing to wait for). The shared
      // waitReason/badge stays for sports where a wait is real (MLB pitchers,
      // NFL/NBA injuries); esports just always pass null.
      waitReason: null,
    });
  }

  const rawCandidateCount = candidates.length;

  // Pass 1: collapse real threshold ladders (series_total across all 3
  // titles, series_handicap for Valorant only -- see ladderTypes) to their
  // single best-edge rung per (market_type, source, match, team/side).
  // map_winner/series_winner are NOT ladders -- each map number or the
  // series itself is its own distinct proposition, same "don't collapse
  // genuinely different outcomes" reasoning as every other sport's
  // non-ladder market types.
  const ladderKey = (row: RecommendedBetRow) =>
    `${row.marketType}|${row.source}|${row.valorantMatchId ?? row.cs2MatchId ?? row.lolMatchId ?? ""}|${row.team ?? ""}|${row.side ?? ""}`;
  const ladderCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of candidates) {
    const key = ladderTypes.has(row.marketType) ? ladderKey(row) : row.key;
    const existing = ladderCollapsed.get(key);
    if (!existing || (row.edge ?? 0) > (existing.edge ?? 0)) {
      ladderCollapsed.set(key, row);
    }
  }
  const ladderDeduped = Array.from(ladderCollapsed.values());

  // Pass 2: same real-world proposition on both platforms (Valorant is the
  // only title with genuine cross-platform overlap right now -- CS2/LoL are
  // Kalshi-only, so this is a no-op for their own rows, same "harmless when
  // not applicable" behavior as every other sport's version of this pass).
  const crossPlatformCollapsed = new Map<string, RecommendedBetRow>();
  for (const row of ladderDeduped) {
    const key = crossPlatformKey(row);
    const existing = crossPlatformCollapsed.get(key);
    if (!existing || preferForCrossPlatformCollapse(row, existing)) {
      crossPlatformCollapsed.set(key, row);
    }
  }
  const deduped = Array.from(crossPlatformCollapsed.values()).sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0));

  // Pass 3: one row per real-world MATCH -- a map_winner Map 1 + Map 2 +
  // series_total row for the SAME match are all downstream of the same
  // team-strength read, same correlation-risk reasoning as every other
  // sport's own capToOneRowPerGame call.
  const gameCapped = capToOneRowPerGame(deduped);

  // Pass 4: portfolio cap -- BOTH pools (weekly + futures), since
  // tournament_winner futures are real, live inventory for at least
  // Valorant/LoL. Same poolCeilings/cumulative-per-pool shape as
  // buildNbaRecommendedBets.
  const poolCeilings = {
    weekly: Math.max(0, weeklyPoolDollars * PORTFOLIO_CEILING_PCT - lockedWeeklyDollars),
    futures: Math.max(0, futuresPoolDollars * PORTFOLIO_CEILING_PCT - lockedFuturesDollars),
  };
  const cumulative = { weekly: 0, futures: 0 };
  const shown: RecommendedBetRow[] = [];
  for (const row of gameCapped) {
    if (cumulative[row.stakePool] + row.suggestedStakeDollars > poolCeilings[row.stakePool]) continue;
    cumulative[row.stakePool] += row.suggestedStakeDollars;
    shown.push(row);
  }

  return {
    rows: shown,
    rawCandidateCount,
    collapsedCount: rawCandidateCount - gameCapped.length,
    cutByPortfolioCapCount: gameCapped.length - shown.length,
  };
}

const VALORANT_LADDER_TYPES = new Set(["series_total", "series_handicap"]);
const CS2_LADDER_TYPES = new Set(["series_total"]);
const LOL_LADDER_TYPES = new Set(["series_total"]);

/** Valorant's own recommended-bets builder -- gets its own independent
 * bankroll pool as of 2026-07-20 (see settings.py::
 * VALORANT_ALLOCATION_PCT_KEY), same as every other sport in this app.
 * Previously combined with CS2/LoL into one shared-pool list
 * (buildEsportsRecommendedBets) -- see this file's git history for that
 * design and why it no longer applies once each title got its own slot. */
export function buildValorantRecommendedBets(
  markets: ValorantMarketRow[],
  weeklyPoolDollars: number,
  futuresPoolDollars: number,
  lockedWeeklyDollars = 0,
  lockedFuturesDollars = 0
): RecommendedBetsResult {
  return buildEsportsTitleRecommendedBets(
    "valorant", markets, (m) => m.valorant_match_id, VALORANT_LADDER_TYPES,
    weeklyPoolDollars, futuresPoolDollars, lockedWeeklyDollars, lockedFuturesDollars
  );
}

/** CS2 equivalent of buildValorantRecommendedBets -- no series_handicap
 * market_type exists for CS2 (see cs2_markets.py::GAME_MARKET_TYPES), so
 * only series_total is a real ladder here. */
export function buildCs2RecommendedBets(
  markets: Cs2MarketRow[],
  weeklyPoolDollars: number,
  futuresPoolDollars: number,
  lockedWeeklyDollars = 0,
  lockedFuturesDollars = 0
): RecommendedBetsResult {
  return buildEsportsTitleRecommendedBets(
    "cs2", markets, (m) => m.cs2_match_id, CS2_LADDER_TYPES,
    weeklyPoolDollars, futuresPoolDollars, lockedWeeklyDollars, lockedFuturesDollars
  );
}

/** LoL equivalent of buildValorantRecommendedBets -- no series_handicap
 * market_type exists for LoL either (see lol_markets.py::GAME_MARKET_TYPES). */
export function buildLolRecommendedBets(
  markets: LolMarketRow[],
  weeklyPoolDollars: number,
  futuresPoolDollars: number,
  lockedWeeklyDollars = 0,
  lockedFuturesDollars = 0
): RecommendedBetsResult {
  return buildEsportsTitleRecommendedBets(
    "lol", markets, (m) => m.lol_match_id, LOL_LADDER_TYPES,
    weeklyPoolDollars, futuresPoolDollars, lockedWeeklyDollars, lockedFuturesDollars
  );
}

/** Collapses each game's two per-team moneyline rows (one binary market per
 * team side, per source) into a single home-team-perspective row -- cleaner
 * to read as a table, same as how a sportsbook shows one moneyline per game. */
export function groupByGameAndSource(rows: MarketRow[]): GameMarketRow[] {
  const grouped = new Map<string, GameMarketRow>();

  for (const row of rows) {
    // Spread/total markets share the same nfl_game_id/game_label/team shape
    // as moneyline (see Phase 4 spread/total build) -- without this filter
    // a home-team-side spread row would silently overwrite the real
    // moneyline row for that (game, source) since both use the same
    // grouping key below. This dashboard table is moneyline-only; spread/
    // total need their own ladder-style display (not built yet -- these
    // markets aren't open on either platform as of this build).
    if (row.market_type !== "moneyline") continue;
    if (!row.nfl_game_id || !row.game_label || !row.gameday || !row.team) continue;
    const [, homeTeam] = row.game_label.split(" @ ");
    if (row.team !== homeTeam) continue; // keep only the home-team-side row per source

    // Prefer final_prob (Elo + news, when a news adjustment is cached) as the
    // "current best estimate" everywhere -- it equals model_prob when there's
    // no news on file, so this is a strict superset, not a behavior change.
    const bestProb = row.final_prob ?? row.model_prob;
    const bestEdge = bestProb !== null && row.implied_prob !== null ? bestProb - row.implied_prob : null;

    const key = `${row.nfl_game_id}:${row.source}`;
    grouped.set(key, {
      key,
      nfl_game_id: row.nfl_game_id,
      game_label: row.game_label,
      gameday: row.gameday,
      source: row.source,
      homeTeam,
      homeImpliedProb: row.implied_prob,
      homeModelProb: row.model_prob,
      homeFinalProb: row.final_prob,
      homeBestProb: bestProb,
      edge: bestEdge,
      volume: row.volume,
      modelValidated: row.model_validated,
      hasNews: row.news_adjustment_pct !== null,
      newsConfidence: row.news_confidence,
      newsRequiresReview: row.news_requires_review,
      noBaselineReason: row.no_baseline_reason,
      predictedHomeScore: row.predicted_home_score,
      predictedAwayScore: row.predicted_away_score,
      lineMovePp: row.line_move_pp,
      kellyFraction: row.kelly_fraction,
      suggestedStakeDollars: row.suggested_stake_dollars,
      suggestedStakeUnits: row.suggested_stake_units,
      stakePool: row.stake_pool,
    });
  }

  return Array.from(grouped.values()).sort((a, b) => {
    if (a.gameday !== b.gameday) return a.gameday.localeCompare(b.gameday);
    return a.game_label.localeCompare(b.game_label);
  });
}

export interface PlacedBetPayload {
  id: number;
  market_id: number;
  market_type: string;
  source: "kalshi" | "polymarket";
  sport: "nfl" | "nba" | "wnba" | "mlb" | "mma" | "tennis" | "soccer" | "valorant" | "cs2" | "lol";
  team: string | null;
  line: number | null;
  side: string | null;
  label: string;
  nfl_game_id: string | null;
  nba_game_id: string | null;
  wnba_game_id: string | null;
  mlb_game_id: string | null;
  mma_fight_id: string | null;
  tennis_match_id: number | null;
  soccer_match_id: number | null;
  valorant_match_id: number | null;
  cs2_match_id: number | null;
  lol_match_id: number | null;
  stake_pool: "weekly" | "futures";
  stake_dollars: number;
  stake_units: number | null;
  market_prob_at_placement: number | null;
  model_prob_at_placement: number | null;
  edge_at_placement: number | null;
  placed_at: string;
  status: "pending" | "won" | "lost" | "push" | "void";
  settled_at: string | null;
  settlement_note: string | null;
  closing_prob: number | null;
  clv_pp: number | null;
  clv_status: "closed" | "pending" | "unavailable" | "not_applicable";
  profit_dollars: number | null;  // realized P/L, null while pending
  profit_units: number | null;
}

export interface PortfolioSportPayload {
  sport: string;
  staked_dollars: number;
  net_profit_dollars: number;
  roi: number | null;
  net_units: number;
  wins: number;
  losses: number;
  pushes: number;
  voids: number;
  pending: number;
  at_risk_dollars: number;
  avg_clv_pp: number | null;
  clv_sample: number;
}

export interface PortfolioSourcePayload {
  source: string;
  staked_dollars: number;
  net_profit_dollars: number;
  roi: number | null;
  net_units: number;
  wins: number;
  losses: number;
  pushes: number;
  voids: number;
  pending: number;
  at_risk_dollars: number;
  avg_clv_pp: number | null;
  clv_sample: number;
}

export interface PortfolioPointPayload {
  date: string;
  cumulative_profit_dollars: number;
  cumulative_profit_units: number;
}

export interface FuturesSummaryPayload {
  staked_dollars: number;
  net_profit_dollars: number;
  roi: number | null;
  net_units: number;
  wins: number;
  losses: number;
  pushes: number;
  voids: number;
  pending: number;
  at_risk_dollars: number;
  by_sport: PortfolioSportPayload[];
}

export interface PortfolioPayload {
  staked_dollars: number;
  net_profit_dollars: number;
  roi: number | null;
  net_units: number;
  wins: number;
  losses: number;
  pushes: number;
  voids: number;
  pending: number;
  at_risk_dollars: number;
  avg_clv_pp: number | null;
  clv_sample: number;
  by_sport: PortfolioSportPayload[];
  by_source: PortfolioSourcePayload[];
  equity_curve: PortfolioPointPayload[];
  futures: FuturesSummaryPayload;
}

export interface OpenBetPayload {
  id: number;
  market_id: number;
  sport: string;
  source: string;
  market_type: string;
  label: string;
  team: string | null;
  side: string | null;
  line: number | null;
  stake_pool: string;
  model_prob_at_placement: number | null;
  stake_dollars: number;
  stake_units: number | null;
  market_prob_at_placement: number | null;
  edge_at_placement: number | null;
  placed_at: string;
  start_time: string | null;
  start_date: string | null;
  original_start_time: string | null;
  rescheduled: boolean;
  clv_status: "closed" | "pending" | "unavailable" | "not_applicable";
}

export async function fetchPortfolio(): Promise<PortfolioPayload> {
  return apiGet<PortfolioPayload>(`/placed-bets/portfolio`);
}

export async function fetchOpenBets(): Promise<OpenBetPayload[]> {
  return apiGet<OpenBetPayload[]>(`/placed-bets/open`);
}

export interface SettledBetPayload {
  id: number;
  market_id: number;
  sport: string;
  source: string;
  market_type: string;
  label: string;
  team: string | null;
  side: string | null;
  line: number | null;
  stake_pool: string;
  stake_dollars: number;
  stake_units: number | null;
  market_prob_at_placement: number | null;
  model_prob_at_placement: number | null;
  status: "won" | "lost" | "push" | "void";
  profit_dollars: number | null;
  profit_units: number | null;
  settled_at: string | null;
  clv_pp: number | null;
  clv_status: "closed" | "pending" | "unavailable" | "not_applicable";
  final_score: string | null;
}

export async function fetchSettledBets(): Promise<SettledBetPayload[]> {
  return apiGet<SettledBetPayload[]>(`/placed-bets/settled`);
}

export interface LockedPoolsPayload {
  weekly_locked_dollars: number;
  futures_locked_dollars: number;
}

export interface CalibrationBucketPayload {
  range_label: string;
  predicted_avg: number | null;
  actual_win_rate: number | null;
  n: number;
}

export interface BetStatsPayload {
  total_settled: number;
  wins: number;
  losses: number;
  pushes: number;
  voids: number;
  win_rate: number | null;
  brier_score: number | null;
  market_brier_score: number | null;
  avg_clv_pp: number | null;
  clv_sample_size: number;
  calibration_buckets: CalibrationBucketPayload[];
}

export async function fetchPlacedBets(status?: string, sport: "nfl" | "nba" | "wnba" | "mlb" | "mma" | "tennis" | "soccer" | "valorant" | "cs2" | "lol" = "nfl"): Promise<PlacedBetPayload[]> {
  const params = new URLSearchParams({ sport });
  if (status) params.set("status", status);
  return apiGet<PlacedBetPayload[]>(`/placed-bets?${params.toString()}`);
}

export async function fetchLockedPools(sport: "nfl" | "nba" | "wnba" | "mlb" | "mma" | "tennis" | "soccer" | "valorant" | "cs2" | "lol" = "nfl"): Promise<LockedPoolsPayload> {
  return apiGet<LockedPoolsPayload>(`/placed-bets/locked?sport=${sport}`);
}

export async function fetchBetStats(sport: "nfl" | "nba" | "wnba" | "mlb" | "mma" | "tennis" | "soccer" | "valorant" | "cs2" | "lol" = "nfl"): Promise<BetStatsPayload> {
  return apiGet<BetStatsPayload>(`/placed-bets/stats?sport=${sport}`);
}

/** Marks a recommended bet as actually placed -- pass the exact row the
 * user clicked so the snapshot captures what they saw at that moment (see
 * PlacedBet's docstring in db/models.py for why this is a snapshot, not a
 * live join). Threads row.sport/nbaGameId/mlbGameId through so a placed bet
 * lands with the right sport + game-id field set, not silently defaulted to
 * "nfl" the way the backend's own PlacedBetIn schema default would produce
 * if this were omitted. */
export async function markBetPlaced(row: RecommendedBetRow): Promise<PlacedBetPayload> {
  return apiPost<PlacedBetPayload>("/placed-bets", {
    market_id: row.marketId,
    market_type: row.marketType,
    source: row.source,
    sport: row.sport,
    team: row.team,
    line: row.line,
    side: row.side,
    label: row.label,
    nfl_game_id: row.nflGameId,
    nba_game_id: row.nbaGameId,
    wnba_game_id: row.wnbaGameId ?? null,
    mlb_game_id: row.mlbGameId,
    mma_fight_id: row.mmaFightId,
    tennis_match_id: row.tennisMatchId,
    soccer_match_id: row.soccerMatchId,
    valorant_match_id: row.valorantMatchId,
    cs2_match_id: row.cs2MatchId,
    lol_match_id: row.lolMatchId,
    stake_pool: row.stakePool,
    stake_dollars: row.suggestedStakeDollars,
    stake_units: row.suggestedStakeUnits,
    market_prob_at_placement: row.impliedProb,
    model_prob_at_placement: row.estProb,
    edge_at_placement: row.edge,
  });
}

/** Mark a FUTURES row placed (season-long/tournament market). Futures have no
 * game/match id, so those all stay null; the season sim's model_prob and the
 * current market price are snapshotted like any other placed bet. Stake falls
 * back to a caller-supplied default when the model didn't size one (many
 * futures are tracking-only / untraded, but a paper position still needs a
 * stake to compute P/L against). Lands in the tracker's separate Futures
 * section. */
export async function markFuturesBetPlaced(
  row: FuturesMarketRow,
  sport: string,
  stakeDollars: number,
  stakeUnits: number | null,
): Promise<PlacedBetPayload> {
  return apiPost<PlacedBetPayload>("/placed-bets", {
    market_id: row.id,
    market_type: row.market_type,
    source: row.source,
    sport,
    team: row.team,
    line: row.line,
    side: row.side,
    label: row.group_label ?? `${sport.toUpperCase()} ${row.market_type}`,
    stake_pool: row.stake_pool ?? "futures",
    stake_dollars: stakeDollars,
    stake_units: stakeUnits,
    market_prob_at_placement: row.implied_prob,
    model_prob_at_placement: row.model_prob,
    edge_at_placement: row.edge,
  });
}

export async function settlePlacedBet(
  id: number,
  status: "won" | "lost" | "push" | "void",
  note?: string
): Promise<PlacedBetPayload> {
  return apiPost<PlacedBetPayload>(`/placed-bets/${id}/settle`, { status, note });
}

export async function deletePlacedBet(id: number): Promise<{ status: string }> {
  return apiDelete(`/placed-bets/${id}`);
}
