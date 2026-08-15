from pydantic import BaseModel


class MarketOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    market_type: str
    source: str
    team: str | None
    nfl_game_id: str | None
    game_label: str | None
    gameday: str | None
    gametime: str | None  # "HH:MM" local kickoff time, may be blank far out -- see NflGame.gametime
    line: float | None
    side: str | None
    implied_prob: float | None
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    volume: float | None
    updated_at: str | None
    model_prob: float | None
    model_validated: bool
    edge: float | None
    news_adjustment_pct: float | None
    news_confidence: str | None
    news_requires_review: bool
    final_prob: float | None
    no_baseline_reason: str | None
    predicted_home_score: float | None  # only populated on moneyline rows -- see app/api/routers/markets.py::_predict_score
    predicted_away_score: float | None
    line_move_pp: float | None  # change in implied_prob over the last LINE_MOVEMENT_HOURS -- see markets.py::_line_movement_pp
    kelly_fraction: float | None  # quarter-Kelly, capped at 5% -- fraction OF THE RELEVANT POOL, see app/models/staking.py
    suggested_stake_dollars: float | None
    suggested_stake_units: float | None
    stake_pool: str | None  # "weekly" | "futures" | None (no stake suggested) -- see staking.py::is_weekly_market_type


class RacingMarketOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int                # Market row id (for paper-logging / CLV tracking)
    series: str            # f1 | irl | nascar
    # Human name of the series this row actually belongs to. Needed because
    # `series` is "nascar" for Cup, Xfinity AND Truck -- Kalshi files all three
    # under one ticker -- so the UI had nothing to show but the sport, and a
    # lower-series bet was indistinguishable from a Cup one. Set from the pool
    # the entrant-list router resolved (racing_markets._resolve_rating_series).
    series_label: str | None = None
    source: str            # kalshi | polymarket
    race_event_id: int | None = None  # links a placed bet to its RaceEvent (start-time + CLV)
    event: str | None      # Kalshi event ticker (one race)
    market_type: str       # race_winner | top_n | pole
    line: int | None       # N for top_n (3/5/10), else None
    driver: str
    implied_prob: float | None
    model_prob: float | None
    model_validated: bool
    edge: float | None
    volume: float | None
    close_time: str | None
    model_note: str | None = None
    kelly_fraction: float | None = None
    suggested_stake_dollars: float | None = None
    suggested_stake_units: float | None = None
    stake_pool: str | None = None


class FuturesMarketOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    market_type: str  # division_winner | conference_champion | one_seed | super_bowl_champion | playoff_qualifier | ...
    source: str
    team: str | None  # None for the league-wide undefeated_season/wins_any markets
    group_label: str | None
    # The COMPETITION, for the tracker's league column -- paper_logger reads
    # row["league"] and, for soccer, maps the division code to a readable name
    # (E0 -> "EPL"). This field did not exist, so every soccer FUTURES paper bet
    # logged a null league and the tracker fell back to the bare sport, the same
    # gap that showed match names in the league column for esports. Optional and
    # defaulted, so the other sports' futures routes (where the sport IS the
    # league, and _league_for_row deliberately returns None) are unaffected.
    league: str | None = None
    # The sport key this row belongs to, when the ROUTE serves more than one.
    # Only /racing/futures sets it: that one endpoint covers f1, irl and nascar,
    # and the cross-sport page needs the real series per row to route its
    # reasoning link and to key a placed bet. Every other futures route serves a
    # single sport, so the caller already knows it and this stays None.
    sport: str | None = None
    line: float | None  # win-total ladder markets only (win_total/exact_win_total/wins_any); None otherwise
    side: str | None  # win_total/wins_any: always "over"; exact_win_total/other futures: None
    implied_prob: float | None
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    volume: float | None
    updated_at: str | None
    model_prob: float | None
    model_validated: bool
    edge: float | None
    kelly_fraction: float | None
    suggested_stake_dollars: float | None
    suggested_stake_units: float | None
    stake_pool: str | None
    line_move_pp: float | None
    # True when this leg's one-winner group has ALREADY been decided (some leg
    # is at/above FUTURES_DECIDED_PRICE). These used to be dropped outright,
    # which meant a settled future silently vanished from the page instead of
    # being shown as finished. Kept and flagged now; never staked.
    group_settled: bool = False
    # The leg that won it, so a settled group reads as a result rather than
    # a list of dead prices.
    group_winner: str | None = None
    # Optional free-text caveat shown under a futures row. Used by the esports
    # tournament sim to flag that a price comes from an Elo-approximated single-
    # elim bracket (real events are double-elim/Swiss) and is shown for tracking
    # only, not staked. Default None -> every other sport's futures unaffected.
    model_note: str | None = None


class MmaMarketOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    market_type: str  # moneyline | distance | method_of_victory | method_of_finish | rounds | round_of_victory
    source: str
    team: str | None  # holds a fighter's real full name (no fixed roster, see market_catalog_mma.py)
    side: str | None  # method_of_victory/method_of_finish: "decision"|"kotko"|"submission"|"draw"; rounds: "under"|"over"
    line: float | None  # rounds: the round-count threshold; round_of_victory: the round number
    fight_label: str | None
    # The CARD this fight is on ("UFC 299: O'Malley vs. Vera 2"). It is MMA's
    # analogue of a league and the tracker's league column had nothing else to
    # show, so every MMA row read just "MMA".
    event_name: str | None = None
    mma_fight_id: str | None
    event_date: str | None
    estimated_start_time: str | None  # full ISO UTC instant, Kalshi's own per-fight estimate -- see poller_mma.py::_infer_start_time_from_kalshi
    weight_class: str | None
    is_title_bout: bool
    scheduled_rounds: int | None
    # Advisory caution, currently used to flag moneylines where a fuller
    # style+defence model disagrees sharply with the Elo price this row is
    # actually built from (see models/mma_model_disagreement.py). Never changes
    # the price, the edge, or the stake -- it is information for the person
    # placing the bet, in the same spirit as flagging a missing MLB pitcher.
    model_note: str | None = None
    implied_prob: float | None
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    volume: float | None
    updated_at: str | None
    model_prob: float | None
    model_validated: bool
    edge: float | None
    no_baseline_reason: str | None
    kelly_fraction: float | None
    suggested_stake_dollars: float | None
    suggested_stake_units: float | None
    stake_pool: str | None


class TennisMarketOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    market_type: str  # moneyline | set_winner | game_spread | game_total | exact_score
    source: str
    team: str | None  # holds a player's real full name (no fixed roster, see market_catalog_tennis.py)
    line: float | None  # set_winner: set number; game_spread/game_total: the line
    side: str | None  # exact_score: "{player_sets}-{opponent_sets}" e.g. "2-1"
    # WHICH SIDE OF THE CONTRACT this row's stake is for: "yes" (buy the outcome)
    # or "no" (buy against it). #195. Distinct from `side` above, which is the
    # OUTCOME SELECTOR. Always "yes" unless (sport, market_type) is in
    # staking.NO_SIDE_CELLS and the NO side is the one carrying the edge --
    # the two are mutually exclusive, a market cannot have edge both ways.
    # model_prob/edge stay in the YES frame so the number means the same thing
    # on every row; the UI reads `position` to say what is being bought.
    position: str = "yes"
    match_label: str | None
    tennis_match_id: int | None
    # Collapses rows that are the SAME real match (see
    # duplicate_fixtures.canonical_tennis_fixture_ids); None when unique.
    fixture_key: int | None = None
    tour: str | None  # atp | wta
    tier: str | None  # tour | challenger | itf
    match_date: str | None
    surface: str | None
    estimated_start_time: str | None  # full ISO UTC instant, live rows only -- see TennisMatch.estimated_start_time
    implied_prob: float | None
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    volume: float | None
    updated_at: str | None
    model_prob: float | None
    model_validated: bool
    edge: float | None
    no_baseline_reason: str | None
    kelly_fraction: float | None
    suggested_stake_dollars: float | None
    suggested_stake_units: float | None
    stake_pool: str | None


class SoccerMarketOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    market_type: str  # moneyline_3way | game_spread | game_total | btts
    source: str
    team: str | None  # real team name for home/away rows and game_spread; null for the draw row, game_total, btts
    side: str | None  # "home" | "draw" | "away" | "over" | "yes"
    line: float | None  # game_spread/game_total only
    match_label: str | None  # "{home_team} vs {away_team}"
    soccer_match_id: int | None
    league: str | None  # football-data.co.uk division code (E0/SP1/I1/D1/F1) or "MLS"
    season: str | None
    match_date: str | None
    estimated_start_time: str | None
    implied_prob: float | None
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    volume: float | None
    updated_at: str | None
    model_prob: float | None
    model_validated: bool
    edge: float | None
    no_baseline_reason: str | None
    # Advisory caution shown beside the price -- currently the cross-division
    # cup-tie warning (see models/cup_match.py CAUTION_NOTE). Not an error:
    # the row is priced and stakeable, the note explains a known residual bias.
    model_note: str | None = None
    kelly_fraction: float | None
    suggested_stake_dollars: float | None
    suggested_stake_units: float | None
    stake_pool: str | None
    news_adjustment_pct: float | None = None  # moneyline_3way only -- injury + motivation blend, see soccer_markets.py
    correct_score_home: int | None = None  # correct_score only
    correct_score_away: int | None = None  # correct_score only


class ValorantMarketOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    market_type: str  # map_winner | series_winner | series_handicap | series_total | tournament_winner
    source: str
    team: str | None  # real team name; null for series_total (game-level) and tournament_winner's group row
    side: str | None  # series_total only: "over"
    line: float | None  # map_winner: the map number; series_handicap: map-margin threshold; series_total: total-maps threshold
    match_label: str | None  # "{team_a} vs {team_b}"
    valorant_match_id: int | None
    # Shared by duplicate rows of the SAME real fixture (a Kalshi row and a
    # Polymarket row spelling a team differently). The frontend's
    # cross-platform dedupe and per-match stake cap key on this instead of
    # the raw match id, which they used to bypass. See duplicate_fixtures.py.
    fixture_key: int | None = None
    event_name: str | None
    match_date: str | None
    estimated_start_time: str | None
    best_of: int | None
    group_label: str | None  # tournament_winner only
    implied_prob: float | None
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    volume: float | None
    updated_at: str | None
    model_prob: float | None
    model_validated: bool
    edge: float | None
    no_baseline_reason: str | None
    kelly_fraction: float | None
    suggested_stake_dollars: float | None
    suggested_stake_units: float | None
    stake_pool: str | None


class Cs2MarketOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    market_type: str  # series_winner | series_total | map_winner | tournament_winner
    source: str
    team: str | None  # real team name; null for series_total (game-level) and tournament_winner's group row
    side: str | None  # series_total only: "over"
    # WHICH SIDE OF THE CONTRACT this row's stake is for: "yes" (buy the outcome)
    # or "no" (buy against it). #195. Distinct from `side` above, which is the
    # OUTCOME SELECTOR. Always "yes" unless (sport, market_type) is in
    # staking.NO_SIDE_CELLS and the NO side is the one carrying the edge --
    # the two are mutually exclusive, a market cannot have edge both ways.
    # model_prob/edge stay in the YES frame so the number means the same thing
    # on every row; the UI reads `position` to say what is being bought.
    position: str = "yes"
    line: float | None  # map_winner: the map number; series_total: total-maps threshold
    match_label: str | None  # "{team_a} vs {team_b}"
    cs2_match_id: int | None
    # Shared by duplicate rows of the SAME real fixture (a Kalshi row and a
    # Polymarket row spelling a team differently). The frontend's
    # cross-platform dedupe and per-match stake cap key on this instead of
    # the raw match id, which they used to bypass. See duplicate_fixtures.py.
    fixture_key: int | None = None
    event_name: str | None
    match_date: str | None
    estimated_start_time: str | None
    best_of: int | None
    group_label: str | None  # tournament_winner only
    implied_prob: float | None
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    volume: float | None
    updated_at: str | None
    model_prob: float | None
    model_validated: bool
    edge: float | None
    no_baseline_reason: str | None
    kelly_fraction: float | None
    suggested_stake_dollars: float | None
    suggested_stake_units: float | None
    stake_pool: str | None


class LolMarketOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    market_type: str  # map_winner | series_total | tournament_winner
    source: str
    team: str | None  # real team name; null for series_total (game-level) and tournament_winner's group row
    side: str | None  # series_total only: "over"
    # WHICH SIDE OF THE CONTRACT this row's stake is for: "yes" (buy the outcome)
    # or "no" (buy against it). #195. Distinct from `side` above, which is the
    # OUTCOME SELECTOR. Always "yes" unless (sport, market_type) is in
    # staking.NO_SIDE_CELLS and the NO side is the one carrying the edge --
    # the two are mutually exclusive, a market cannot have edge both ways.
    # model_prob/edge stay in the YES frame so the number means the same thing
    # on every row; the UI reads `position` to say what is being bought.
    position: str = "yes"
    line: float | None  # map_winner: the map number; series_total: total-maps threshold
    match_label: str | None  # "{team_a} vs {team_b}"
    lol_match_id: int | None
    # Shared by duplicate rows of the SAME real fixture (a Kalshi row and a
    # Polymarket row spelling a team differently). The frontend's
    # cross-platform dedupe and per-match stake cap key on this instead of
    # the raw match id, which they used to bypass. See duplicate_fixtures.py.
    fixture_key: int | None = None
    event_name: str | None
    match_date: str | None
    estimated_start_time: str | None
    best_of: int | None
    group_label: str | None  # tournament_winner only
    implied_prob: float | None
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    volume: float | None
    updated_at: str | None
    model_prob: float | None
    model_validated: bool
    edge: float | None
    no_baseline_reason: str | None
    kelly_fraction: float | None
    suggested_stake_dollars: float | None
    suggested_stake_units: float | None
    stake_pool: str | None


class ReasoningFactorOut(BaseModel):
    label: str
    detail: str


class ReasoningOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    market_id: int
    market_type: str
    label: str
    model_prob: float | None
    market_prob: float | None
    edge: float | None
    insight: str
    methodology: str
    factors: list[ReasoningFactorOut]
    caveats: list[str]

