import datetime

from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, UniqueConstraint, Index, Boolean

from app.db.database import Base


class NflGame(Base):
    __tablename__ = "nfl_games"

    id = Column(String, primary_key=True)  # nflverse game_id, e.g. "2026_01_NE_SEA"
    season = Column(Integer, nullable=False)
    week = Column(Integer, nullable=False)
    game_type = Column(String, nullable=False)
    gameday = Column(String, nullable=False)  # ISO date, e.g. "2026-09-09"
    gametime = Column(String)  # "HH:MM" local kickoff time, may be blank far out
    away_team = Column(String, nullable=False)
    home_team = Column(String, nullable=False)
    away_score = Column(Integer, nullable=True)
    home_score = Column(Integer, nullable=True)
    # HALF-time scores, needed to grade the 1H/2H winner markets (KXNFL1H /
    # KXNFL2H). nflverse -- this app's NFL schedule/score source -- publishes
    # only the final, so these come from ESPN's per-quarter linescores
    # (espn_client.fetch_half_scores). Null until that runs for a played game.
    # Second-half goals are DERIVED as final minus half, never stored twice.
    away_score_1h = Column(Integer, nullable=True)
    home_score_1h = Column(Integer, nullable=True)
    # Historical/closing sportsbook lines from nflverse, present once a game has aggregated odds data
    spread_line = Column(Float, nullable=True)
    total_line = Column(Float, nullable=True)
    home_moneyline = Column(Integer, nullable=True)
    away_moneyline = Column(Integer, nullable=True)
    # Starting QB names, used to match against the free ESPN injury feed
    home_qb_name = Column(String, nullable=True)
    away_qb_name = Column(String, nullable=True)
    # Situational fields nflverse already publishes but weren't persisted until
    # the rest/travel + divisional-squeeze + weather adjustments were added
    away_rest = Column(Integer, nullable=True)  # days since each team's last game
    home_rest = Column(Integer, nullable=True)
    div_game = Column(Integer, nullable=True)  # 0/1, sqlite has no bool
    roof = Column(String, nullable=True)  # "outdoors" | "dome" | "closed" | "open" (retractable)
    # Coach names, already published by nflverse for future games (coaching
    # staffs are set well ahead of the season, unlike QB names) -- used to
    # detect an in-season coaching change by diffing against a team's earlier
    # games this season.
    home_coach = Column(String, nullable=True)
    away_coach = Column(String, nullable=True)
    # nflverse's own Home/Neutral flag (international series, relocated
    # games) and venue name -- used to zero out the home-field-advantage
    # credit for neutral-site games (see elo.py effective_home_field_adv).
    location = Column(String, nullable=True)
    stadium = Column(String, nullable=True)
    # nflverse's own field ("grass"/"fieldturf"/"astroturf"/... -- see
    # game_lines.py's TURF_TOTAL_BOOST_PTS docstring for the real, dome-
    # controlled data check behind using this for the totals model).
    surface = Column(String, nullable=True)


class NbaGame(Base):
    __tablename__ = "nba_games"

    id = Column(String, primary_key=True)  # ESPN event id, e.g. "401705127"
    season = Column(Integer, nullable=False)  # ESPN convention: labeled by the season's ENDING year (Jan 2025 game -> season=2025)
    game_type = Column(String, nullable=False)  # PRE | REG | POST, from ESPN's season.type
    gameday = Column(String, nullable=False)  # ISO date, e.g. "2026-10-21"
    gametime = Column(String)  # "HH:MM" UTC tip-off time, may be blank far out
    away_team = Column(String, nullable=False)
    home_team = Column(String, nullable=False)
    away_score = Column(Integer, nullable=True)
    home_score = Column(Integer, nullable=True)
    # Derived directly from the pulled schedule itself (days since each
    # team's previous game) -- no extra network call, same reasoning as
    # nflverse's away_rest/home_rest. Back-to-backs are a much stronger,
    # better-documented effect in the NBA than NFL's weekly rest gaps, so
    # this is expected to matter more here than it did for NFL.
    away_rest = Column(Integer, nullable=True)
    home_rest = Column(Integer, nullable=True)
    # ESPN's own neutralSite flag (Mexico City/Paris/NBA Cup-final games) --
    # same "zero out home-court credit" use as NflGame.location.
    location = Column(String, nullable=True)  # "Home" | "Neutral"
    arena = Column(String, nullable=True)
    # NOTE: unlike nflverse's games.csv, ESPN does not publish historical
    # closing spread/total/moneyline odds -- there is no NBA equivalent of
    # NflGame.spread_line/total_line/home_moneyline/away_moneyline here.
    # Phase 6 (backtest validation) will need a separate free historical-odds
    # source; not solved yet, flagged rather than silently omitted.


class WnbaGame(Base):
    """One WNBA game from ESPN's public scoreboard (parallel to NbaGame, but
    seasons are labeled by their single calendar year -- May-Oct -- not the
    NBA's ending-year convention). Backs elo_service_wnba's moneyline model."""

    __tablename__ = "wnba_games"

    id = Column(String, primary_key=True)  # ESPN event id
    season = Column(Integer, nullable=False)  # calendar year
    game_type = Column(String, nullable=False)  # PRE | REG | POST
    gameday = Column(String, nullable=False)  # ISO date
    gametime = Column(String)  # "HH:MM" UTC tip-off, may be blank far out
    away_team = Column(String, nullable=False)
    home_team = Column(String, nullable=False)
    away_score = Column(Integer, nullable=True)
    home_score = Column(Integer, nullable=True)
    away_rest = Column(Integer, nullable=True)
    home_rest = Column(Integer, nullable=True)
    location = Column(String, nullable=True)  # "Home" | "Neutral"
    arena = Column(String, nullable=True)


class CfbGame(Base):
    """One FBS college-football game from ESPN's public scoreboard (parallel to
    WnbaGame). Backs elo_service_cfb's moneyline model.

    Teams are keyed by ESPN ABBREVIATION (e.g. "ND", "UGA", "MIZ"), matching
    data/cfb_game_cache.json which the Elo constants were derived from, and
    matching what market_matcher_cfb resolves Kalshi tickers to.

    `season` follows the cache's convention: a January game (bowls, playoff
    final) belongs to the PREVIOUS calendar year's season, so the 2026-01-19
    title game is season 2025. market_matcher_cfb._season_for encodes the same
    rule -- they must agree or January markets link to nothing.

    `neutral` matters here in a way it doesn't for most sports: bowls, kickoff
    classics and conference championships are played at neutral sites where the
    home designation is a bracket artifact, and elo_cfb zeroes home-field
    advantage for them.
    """

    __tablename__ = "cfb_games"

    id = Column(String, primary_key=True)  # ESPN event id
    season = Column(Integer, nullable=False)  # see docstring: Jan games -> prior year
    game_type = Column(String, nullable=False)  # REG | POST
    gameday = Column(String, nullable=False)  # ISO date
    gametime = Column(String)  # "HH:MM" UTC kickoff, may be blank far out
    away_team = Column(String, nullable=False)  # ESPN abbreviation
    home_team = Column(String, nullable=False)  # ESPN abbreviation
    away_score = Column(Integer, nullable=True)
    home_score = Column(Integer, nullable=True)
    neutral = Column(Integer, nullable=False, default=0)  # 1 = neutral site
    venue = Column(String, nullable=True)


class MlbGame(Base):
    __tablename__ = "mlb_games"

    id = Column(String, primary_key=True)  # MLB Stats API gamePk, e.g. "778563"
    season = Column(Integer, nullable=False)
    game_type = Column(String, nullable=False)  # R | S | F | D | L | W | A -- see mlb_data.py
    game_number = Column(Integer, nullable=False, default=1)  # disambiguates doubleheaders sharing gameday
    gameday = Column(String, nullable=False)  # ISO date, local game date (officialDate)
    gametime = Column(String)  # "HH:MM" UTC start time, may be blank far out
    away_team = Column(String, nullable=False)
    home_team = Column(String, nullable=False)
    away_score = Column(Integer, nullable=True)
    home_score = Column(Integer, nullable=True)
    away_probable_pitcher = Column(String, nullable=True)
    home_probable_pitcher = Column(String, nullable=True)
    away_probable_pitcher_id = Column(Integer, nullable=True)
    home_probable_pitcher_id = Column(Integer, nullable=True)
    away_rest = Column(Integer, nullable=True)
    home_rest = Column(Integer, nullable=True)
    venue = Column(String, nullable=True)
    # MLB plays a handful of real neutral-site games/season (London, Mexico
    # City, Korea) -- not yet populated (MLB Stats API's schedule response
    # doesn't carry an explicit neutral-site flag the way ESPN's does for
    # NFL/NBA; needs a small hardcoded venue-name lookup, deferred until the
    # situational/structural layer is built), kept nullable so elo_mlb.py's
    # NEUTRAL_SITE_HOME_FIELD_ADV path has somewhere to read from once it is.
    location = Column(String, nullable=True)


class MmaFight(Base):
    """One UFC fight (not a "game" -- no home/away, no season/week). Sourced
    from ufcstats.com (see app/clients/ufcstats_client.py), both completed
    AND upcoming (its /statistics/events/upcoming page lists real scheduled
    cards weeks ahead, confirmed live 2026-07-17 -- same role nflverse's
    future-game rows play for NFL).

    fighter_a_id/fighter_b_id are RAW ufcstats page order, not a
    deliberately-neutralized split -- CORRECTED 2026-07-18 (an earlier
    version of this docstring claimed page order was "neutral," avoiding a
    98%+ winner-correlation leak the earlier standalone ufc-model research
    project found in some public datasets; that earlier project's OWN
    a/b split may well have been neutralized, but THIS app's actual
    `ufc_data.py::pair_fight_rows` just takes `a, b = rows` in whatever
    order ufcstats' fight-details page lists the two
    `b-fight-details__person` divs -- confirmed live: fighter_a is the
    winner in 64.2% of decided fights, not the ~50% a truly neutral split
    would give (ufcstats visually lists the winner first on a completed
    fight's page). This is HARMLESS for order-invariant targets
    (went_the_distance/method_of_finish don't depend on who wins, just how
    the fight ends), but ANY future check correlating a raw per-fighter-a
    binary against outcome/residual MUST symmetrize (stack both fighters'
    own perspectives) or it will inherit this bias -- see
    scripts/check_mma_round2_signals.py's stance-matchup check for a real
    example of this bug being caught and fixed. winner_id is the single
    unambiguous source of truth for who won, resolved via ufcstats' own
    stable per-fighter URL id, not name or table position."""

    __tablename__ = "mma_fights"

    id = Column(String, primary_key=True)  # ufcstats fight-details URL id, e.g. "701c97405da76603"
    event_id = Column(String, nullable=False)  # ufcstats event-details URL id
    event_name = Column(String, nullable=False)
    event_date = Column(String, nullable=False)  # ISO date, e.g. "2026-07-18"
    estimated_start_time = Column(String, nullable=True)  # full ISO UTC instant -- Kalshi's own real, per-fight occurrence_datetime estimate (staggered across the card, not a flat event-level time), see poller_mma.py::_infer_start_time_from_kalshi. Genuinely an ESTIMATE (fight order can reshuffle) -- kept fresh every poll, not backfilled-once like scheduled_rounds.
    weight_class = Column(String, nullable=True)
    is_title_bout = Column(Integer, nullable=False, default=0)  # 0/1, sqlite has no bool
    fighter_a_id = Column(String, nullable=False)
    fighter_a_name = Column(String, nullable=False)
    fighter_b_id = Column(String, nullable=False)
    fighter_b_name = Column(String, nullable=False)
    winner_id = Column(String, nullable=True)  # null: not yet fought, OR a real draw/no-contest
    method = Column(String, nullable=True)  # e.g. "KO/TKO", "Submission", "Decision - Unanimous"
    round = Column(Integer, nullable=True)
    time = Column(String, nullable=True)  # "MM:SS" within the finishing round
    scheduled_rounds = Column(Integer, nullable=True)  # 3 (standard) or 5 (title/main-event)
    # Derived once the result is known: method starts with "Decision" -- see
    # ufc_data.py::WENT_THE_DISTANCE_METHODS. This is the one market this
    # app's earlier, separate research (project_ufc_betting_model) found a
    # real walk-forward edge on; being re-validated fresh in THIS app's own
    # harness, not ported, before it's treated as confirmed here.
    went_the_distance = Column(Integer, nullable=True)  # 0/1/null


class TennisMatch(Base):
    """One tennis match (not a "game" -- no home/away, no season/week, like
    MmaFight). Two heterogeneous free sources feed this table (see
    app/ingestion/tennis_data.py):
      - tennis-data.co.uk (tour-level ATP/WTA only, xlsx per year, has
        surface + point-in-time WRank/WPts + real bookmaker odds) --
        confirmed live 2026-07-18 still serving current data (through
        2026-07-12), ATP back to 2000, WTA back to 2007.
      - tennisexplorer.com (Challenger + ITF, scraped day-by-day results
        pages) -- a source NOT used by this user's earlier standalone
        tennis-model project, which is why that project found "zero
        Challenger/ITF coverage." Confirmed live 2026-07-18: no bot gate,
        real historical odds embedded (`coursew`/`course` columns) at
        Challenger AND ITF level back to at least 2018, closing the exact
        gap the standalone project flagged as unsolvable.

    player_a_key/player_b_key are NORMALIZED "surname initial." strings
    (e.g. "djokovic n."), NOT a stable numeric id -- both sources display
    names in this exact format on their match-level pages, so this is the
    cheapest reliable cross-source join key, but it IS a real, documented
    simplification: two different players sharing surname + first initial
    within the same tour/gender collide. Same category of known tradeoff as
    this app's NFL backup-QB name matching (first-initial+last-name). Not
    fixed here -- would need a much heavier fuzzy/compound-surname resolver
    (like the separate Kalshi/Polymarket tennis research project built) to
    close completely, and tour-level collisions are rare in practice (two
    contemporaneous ATP/WTA-ranked pros with the same surname+initial are
    unusual, though not impossible).

    tier is "tour" | "challenger" | "itf" -- NOT "level" (tennis-data.co.uk's
    own "Series" column already means something narrower, ATP250 vs Masters
    1000 etc; that finer tournament-tier detail is preserved separately in
    tourney_name/round for anything that wants it, tier here is only the
    coarse tour/challenger/itf split that determines training-data depth
    and whether real historical odds exist for backtesting)."""

    __tablename__ = "tennis_matches"
    __table_args__ = (UniqueConstraint("source", "source_match_id", name="uq_tennis_source_match"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String, nullable=False)  # "tennisdata" | "tennisexplorer"
    source_match_id = Column(String, nullable=False)  # source's own row/URL id, for de-dupe on re-ingest
    tour = Column(String, nullable=False)  # "atp" | "wta"
    tier = Column(String, nullable=False)  # "tour" | "challenger" | "itf"
    tourney_name = Column(String, nullable=False)
    surface = Column(String, nullable=True)  # "Hard" | "Clay" | "Grass" | "Carpet" -- null when genuinely unknown, never guessed
    round = Column(String, nullable=True)
    best_of = Column(Integer, nullable=True)  # 3 or 5 -- only ATP Grand Slams are best-of-5, everything else (WTA at any level, Challenger/ITF) is best-of-3
    match_date = Column(String, nullable=False)  # ISO date
    estimated_start_time = Column(String, nullable=True)  # full ISO UTC instant -- Kalshi's occurrence_datetime / Polymarket's gameStartTime, whichever platform's poller sees it first (live rows only, see poller_tennis.py); a genuine ESTIMATE, kept fresh every poll while winner_key is still null, same "always overwrite while upcoming" pattern as MmaFight.estimated_start_time
    # Kalshi's expected_expiration_time for this match's market. Kalshi does NOT
    # revise occurrence_datetime when a match is rescheduled, but it DOES revise
    # this. When the two disagree on the DATE, the stored start is stale and must
    # not be trusted -- see tennis_markets.py. Verified on Fritz vs Jodar:
    # occurrence 2026-08-02T21:30Z (stale) vs expiration 2026-08-03T21:50Z.
    expected_expiration_time = Column(String, nullable=True)
    # Which source last set estimated_start_time. tennisexplorer tracks the real
    # order of play and WINS over Kalshi's occurrence_datetime, which is never
    # revised. Without this the two writers fought every poll -- tennisexplorer
    # wrote the correct time, the Kalshi step overwrote it moments later, and
    # matches flickered in and out of the recommended list.
    start_time_source = Column(String, nullable=True)
    player_a_key = Column(String, nullable=False)  # normalized "surname i." -- see class docstring
    player_a_name = Column(String, nullable=False)  # display name as the source rendered it
    player_b_key = Column(String, nullable=False)
    player_b_name = Column(String, nullable=False)
    winner_key = Column(String, nullable=True)  # null: not yet played, or a real walkover/retirement excluded from scoring
    is_retirement = Column(Integer, nullable=False, default=0)  # 0/1 -- real play happened but didn't finish; still has a winner_key, excluded from Elo the same way MmaFight excludes no-contests
    score = Column(String, nullable=True)
    # Point-in-time rank/rank-points, tennis-data.co.uk ONLY (tennisexplorer's
    # results pages don't expose these) -- null for tennisexplorer-sourced
    # Challenger/ITF rows, never backfilled from a later/different snapshot.
    player_a_rank = Column(Integer, nullable=True)
    player_b_rank = Column(Integer, nullable=True)
    # Real historical bookmaker odds (decimal), when the source has them --
    # tennis-data.co.uk (Bet365/Pinnacle) for tour level, tennisexplorer's
    # own embedded odds for Challenger/ITF. Used for the go/no-go backtest
    # gate; null rows are simply excluded from that check, not imputed.
    player_a_odds = Column(Float, nullable=True)
    player_b_odds = Column(Float, nullable=True)


class SoccerMatch(Base):
    """One soccer match (home/away, unlike TennisMatch/MmaFight). Two
    heterogeneous sources feed this table (see app/ingestion/soccer_data.py):
      - football-data.co.uk (EPL/La Liga/Serie A/Bundesliga/Ligue 1, division
        codes E0/SP1/I1/D1/F1 -- confirmed live 2026-07-19, E0 back to
        1993/94, real opening+closing bookmaker odds including 3-way
        moneyline and O/U 2.5 goals) -- backtestable.
      - ESPN's free public scoreboard API (site.api.espn.com, MLS/"usa.1" --
        confirmed live 2026-07-19, real historical scores by date range) --
        results only, NO odds, so MLS rows never get home_odds/draw_odds/
        away_odds populated and can never clear the backtest gate. Ratings
        still get fit from real goals data; model_validated stays False
        permanently for MLS until this app tracks enough of its own settled
        bets to validate a different way (see elo_service_soccer.py).

    football-data.co.uk's own fixtures.csv feed was checked live 2026-07-19
    and found too thin/short-horizon to serve as a real external schedule
    (~12 rows, next-matchday-only, doesn't cover an off-season league at
    all) -- so, like TennisMatch, upcoming/live rows for EVERY league here
    are instead derived directly from whichever platform's own listing shows
    up first (see market_catalog_soccer.py::find_or_create_upcoming_match),
    with the other platform's poller matching onto that same row by team
    name rather than creating a duplicate.

    league is football-data.co.uk's own division code (E0/SP1/I1/D1/F1) or
    the literal string "MLS" -- NOT a display name, to match the join key
    build_soccer_match_cache.py uses when writing the historical cache."""

    __tablename__ = "soccer_matches"
    __table_args__ = (UniqueConstraint("source", "source_match_id", name="uq_soccer_source_match"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String, nullable=False)  # "football-data.co.uk" | "espn" | "live"
    source_match_id = Column(String, nullable=False)  # source's own row id, for de-dupe on re-ingest
    league = Column(String, nullable=False)  # "E0" | "SP1" | "I1" | "D1" | "F1" | "MLS"
    season = Column(String, nullable=False)  # "2025-2026"
    match_date = Column(String, nullable=False)  # ISO date
    estimated_start_time = Column(String, nullable=True)  # live-derived rows only, same "always overwrite while upcoming" pattern as TennisMatch/MmaFight
    home_team = Column(String, nullable=False)
    away_team = Column(String, nullable=False)
    home_goals_ft = Column(Integer, nullable=True)  # null until played
    away_goals_ft = Column(Integer, nullable=True)
    home_goals_ht = Column(Integer, nullable=True)
    away_goals_ht = Column(Integer, nullable=True)
    # Which side scored FIRST ('H'/'A'), or 'N' for a goalless match. NULL
    # means unknown -- ftts bets on such a match stay pending rather than
    # being guessed. Populated from ESPN scoring plays; a final score alone
    # cannot answer this, which is why 2 ftts bets sat permanently unsettled.
    first_scorer = Column(String, nullable=True)
    # Which writer set estimated_start_time. Mirrors TennisMatch's column of
    # the same name and exists for the same reason: two writers race for this
    # field and the platform's is the untrustworthy one. See
    # market_catalog_soccer.update_match_estimated_start_time.
    start_time_source = Column(String, nullable=True)
    result_ft = Column(String, nullable=True)  # "H" | "D" | "A" -- null until played
    # Real historical closing odds (decimal), football-data.co.uk only --
    # used for the go/no-go backtest gate; null rows (MLS, or any not-yet-
    # played match) are simply excluded from that check, never imputed.
    home_odds = Column(Float, nullable=True)
    draw_odds = Column(Float, nullable=True)
    away_odds = Column(Float, nullable=True)


class ValorantMatch(Base):
    """One Valorant esports series (Bo1/Bo3/Bo5) -- not a "game" -- no home/
    away, no season/week, same shape as MmaFight/TennisMatch. Sourced from
    vlr.gg (see app/ingestion/valorant_data.py), confirmed live 2026-07-19:
    loads with zero Cloudflare/bot gating (unlike HLTV, the analogous CS2
    site), full VCT 2026 schedule visible with real team names/times.

    Like TennisMatch/SoccerMatch, vlr.gg's own live schedule listing IS the
    schedule (see market_catalog_valorant.py::find_or_create_upcoming_match)
    -- upcoming/live rows are derived from whichever platform's poller (or
    the vlr.gg scrape itself) sees the match first.

    best_of is NOT always known upfront from vlr.gg's schedule page alone
    (only the match detail page states it reliably) -- same "infer from the
    real market ladder rather than guess" pattern as MmaFight.scheduled_rounds
    being backfilled from Kalshi's own KXUFCROUNDS ladder (see
    poller_mma.py::_infer_scheduled_rounds_from_kalshi): here, the highest
    map_number actually seen across KXVALORANTMAP markets for this match is
    a real lower bound on best_of, backfilled the same way once markets are
    polled, never guessed ahead of time."""

    __tablename__ = "valorant_matches"
    __table_args__ = (UniqueConstraint("source", "source_match_id", name="uq_valorant_source_match"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String, nullable=False)  # "vlr"
    source_match_id = Column(String, nullable=False)  # vlr.gg match URL id, or "live:..." synthetic id -- see ValorantMatch class docstring
    event_name = Column(String, nullable=False)
    match_date = Column(String, nullable=False)  # ISO date
    estimated_start_time = Column(String, nullable=True)  # live-derived, "always overwrite while upcoming" pattern -- same as MmaFight/TennisMatch/SoccerMatch
    start_time_source = Column(String, nullable=True)
    team_a = Column(String, nullable=False)
    team_b = Column(String, nullable=False)
    best_of = Column(Integer, nullable=True)  # 1, 3, or 5 -- see class docstring for why this is often backfilled rather than known upfront
    maps_won_a = Column(Integer, nullable=True)  # null until played
    maps_won_b = Column(Integer, nullable=True)
    winner = Column(String, nullable=True)  # "team_a" | "team_b" | null (not yet decided)


class ValorantMap(Base):
    """One individual map within a ValorantMatch series -- e.g. Map 2 of a
    Bo3. Needed as its own table (not just columns on ValorantMatch) because
    a real, live market type (Kalshi KXVALORANTMAP, confirmed live
    2026-07-19: 20 open markets across real VCT matches) prices each map
    individually, and best_of varies match to match (up to 5 rows)."""

    __tablename__ = "valorant_maps"
    __table_args__ = (UniqueConstraint("valorant_match_id", "map_number", name="uq_valorant_match_map"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    valorant_match_id = Column(Integer, ForeignKey("valorant_matches.id"), nullable=False)
    map_number = Column(Integer, nullable=False)  # 1-5
    map_name = Column(String, nullable=True)  # e.g. "Ascent" -- null until known (vlr.gg only reveals map picks once played/near-live)
    team_a_score = Column(Integer, nullable=True)  # round score within this map, null until played
    team_b_score = Column(Integer, nullable=True)
    winner = Column(String, nullable=True)  # "team_a" | "team_b" | null


class Cs2Match(Base):
    """One CS2 esports series (Bo1/Bo3/Bo5) -- parallel to ValorantMatch, but
    sourced from liquipedia.net (see app/ingestion/cs2_data.py) instead of
    vlr.gg. Confirmed live 2026-07-19: liquipedia.net's counterstrike wiki
    loads with ZERO Cloudflare/bot gating (the earlier standalone CS2
    betting-model project's block was HLTV-specific, not CS2-wide).

    UNLIKE ValorantMatch, best_of IS known upfront here -- Liquipedia's own
    schedule listing states "(Bo3)"/"(Bo5)" directly (see cs2_data.py's
    module docstring), no ladder-market backfill needed."""

    __tablename__ = "cs2_matches"
    __table_args__ = (UniqueConstraint("source", "source_match_id", name="uq_cs2_source_match"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String, nullable=False)  # "liquipedia"
    source_match_id = Column(String, nullable=False)  # synthetic id built from real Liquipedia slugs+timestamp, or "live:..." -- see cs2_data.py
    event_name = Column(String, nullable=False)
    match_date = Column(String, nullable=False)  # ISO date
    estimated_start_time = Column(String, nullable=True)  # real UNIX-epoch-derived UTC instant from Liquipedia's own timer widget -- NOT a rough guess the way ValorantMatch's often is, see cs2_data.py
    start_time_source = Column(String, nullable=True)
    team_a = Column(String, nullable=False)
    team_b = Column(String, nullable=False)
    best_of = Column(Integer, nullable=True)  # known upfront from Liquipedia's listing (see class docstring); nullable only for defensive parsing gaps
    maps_won_a = Column(Integer, nullable=True)
    maps_won_b = Column(Integer, nullable=True)
    winner = Column(String, nullable=True)  # "team_a" | "team_b" | null


class Cs2Map(Base):
    """One individual map within a Cs2Match series -- parallel to
    ValorantMap. KXCS2MAPWINNER (the Kalshi series that would price these
    individually) has zero real open markets as of 2026-07-19 (see
    kalshi_cs2_client.py's own docstring) -- this table exists for when that
    changes, same "ready but currently unpopulated" status as the client
    function itself."""

    __tablename__ = "cs2_maps"
    __table_args__ = (UniqueConstraint("cs2_match_id", "map_number", name="uq_cs2_match_map"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    cs2_match_id = Column(Integer, ForeignKey("cs2_matches.id"), nullable=False)
    map_number = Column(Integer, nullable=False)
    map_name = Column(String, nullable=True)
    team_a_score = Column(Integer, nullable=True)
    team_b_score = Column(Integer, nullable=True)
    winner = Column(String, nullable=True)


class CodMatch(Base):
    """One Call of Duty series -- parallel to Cs2Match/ValorantMatch/LolMatch,
    sourced from breakingpoint.gg's tRPC API (see
    scripts/build_cod_match_cache_bp.py and app/ingestion/cod_data.py).

    NOT Liquipedia, deliberately. The first CoD crawler used Liquipedia and got
    this app's IP banned site-wide, which took live CS2 fixture ingestion down
    with it. breakingpoint.gg is also the better source on its merits: JSON
    rather than wikitext, REAL team names rather than "tx"/"mia" shortcodes
    (so market joins need no shortcode-resolution step at all), best_of and the
    winner supplied directly, and a status-counts endpoint that states the
    expected total so a truncated crawl is detectable.

    There is no CodMap sibling. Kalshi's KXCODGAME lists match_winner only --
    no per-map markets exist to price, and the model's per-map probability is
    the same number for every map anyway (see elo_cod.SeriesDistribution.
    prob_map_n_win_a), which is the flaw already gated in the other titles."""

    __tablename__ = "cod_matches"
    __table_args__ = (UniqueConstraint("source", "source_match_id", name="uq_cod_source_match"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String, nullable=False)  # "breakingpoint" or "live:..."
    source_match_id = Column(String, nullable=False)
    event_name = Column(String, nullable=True)  # nullable: live rows are seen before their event is named
    match_date = Column(String, nullable=False)  # ISO date
    estimated_start_time = Column(String, nullable=True)
    team_a = Column(String, nullable=False)
    team_b = Column(String, nullable=False)
    best_of = Column(Integer, nullable=True)  # real value from the source; CDL is Bo5, Esports World Cup Bo7
    maps_won_a = Column(Integer, nullable=True)
    maps_won_b = Column(Integer, nullable=True)
    winner = Column(String, nullable=True)  # "team_a" | "team_b" | null

    # THE SOURCE TELLS US, we do not infer it. Unique among this app's sports:
    # every other one decides "has it started?" by comparing a platform start
    # time against the clock, and that failed twice on 2026-08-09 -- a live
    # soccer match recommended at 1-0 down, and a live CoD match (Heretics 2-0
    # Falcons) whose Kalshi occurrence_datetime was four hours late. The router
    # gates on this flag directly; the clock stays as a backstop.
    is_live = Column(Boolean, nullable=False, default=False)


class LolMatch(Base):
    """One League of Legends esports series (Bo1/Bo3/Bo5) -- parallel to
    Cs2Match/ValorantMatch, sourced from Leaguepedia's Cargo API (see
    app/ingestion/lol_data.py) rather than plain HTML scraping. Leaguepedia
    (lol.fandom.com, a Fandom wiki) DOES expose a real, working
    action=cargoquery API endpoint (confirmed live 2026-07-19 -- it returned
    a rate-limit error, not an "unrecognized action" error the way
    Liquipedia's identical query did, proving the endpoint is real and just
    needs polite pacing), unlike Liquipedia's counterstrike wiki which has no
    Cargo API at all."""

    __tablename__ = "lol_matches"
    __table_args__ = (UniqueConstraint("source", "source_match_id", name="uq_lol_source_match"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String, nullable=False)  # "leaguepedia"
    source_match_id = Column(String, nullable=False)
    event_name = Column(String, nullable=False)
    match_date = Column(String, nullable=False)  # ISO date
    estimated_start_time = Column(String, nullable=True)
    start_time_source = Column(String, nullable=True)
    team_a = Column(String, nullable=False)
    team_b = Column(String, nullable=False)
    best_of = Column(Integer, nullable=True)
    maps_won_a = Column(Integer, nullable=True)
    maps_won_b = Column(Integer, nullable=True)
    winner = Column(String, nullable=True)  # "team_a" | "team_b" | null


class LolMap(Base):
    """One individual map within a LolMatch series -- parallel to
    Cs2Map/ValorantMap. Real, live Kalshi inventory (KXLOLMAP, confirmed
    2026-07-19: 24 open markets) prices these individually, same shape as
    Valorant's KXVALORANTMAP."""

    __tablename__ = "lol_maps"
    __table_args__ = (UniqueConstraint("lol_match_id", "map_number", name="uq_lol_match_map"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    lol_match_id = Column(Integer, ForeignKey("lol_matches.id"), nullable=False)
    map_number = Column(Integer, nullable=False)
    map_name = Column(String, nullable=True)
    team_a_score = Column(Integer, nullable=True)
    team_b_score = Column(Integer, nullable=True)
    winner = Column(String, nullable=True)


class Cs2RosterChangeCache(Base):
    """Cached result of roster_changes_cs2.py's Liquipedia Portal:Transfers
    scrape -- one row per real team with a roster change inside its own
    LOOKBACK_DAYS window, keyed by team_name (not a match id -- a team can
    have a real roster change while having no upcoming/live match at all,
    same "cache the raw fact, not a per-match join" reasoning as
    NbaCoachSnapshot). Recomputed every poll cycle, same cadence as this
    title's own NewsAdjustmentCache-equivalent would be, but there is no
    probability adjustment here -- see roster_changes_cs2.py's own
    docstring on why this stays a pure informational caveat."""

    __tablename__ = "cs2_roster_change_cache"

    team_name = Column(String, primary_key=True)
    change_date = Column(String, nullable=False)  # ISO date, real
    detail = Column(String, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)


class ValorantRosterChangeCache(Base):
    """Valorant's own version of Cs2RosterChangeCache -- parallel structure,
    sourced from roster_changes_valorant.py's vlr.gg /transfers scrape."""

    __tablename__ = "valorant_roster_change_cache"

    team_name = Column(String, primary_key=True)
    change_date = Column(String, nullable=False)
    detail = Column(String, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)


class LolRosterChangeCache(Base):
    """LoL's own version of Cs2RosterChangeCache -- parallel structure,
    sourced from roster_changes_lol.py's Leaguepedia Tenures Cargo query."""

    __tablename__ = "lol_roster_change_cache"

    team_name = Column(String, primary_key=True)
    change_date = Column(String, nullable=False)
    detail = Column(String, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)


class Market(Base):
    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("source", "source_ticker", name="uq_source_ticker"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    # "nfl" | "nba" -- added when NBA became the 2nd sport (2026-07-16).
    # Needed because per-sport game_id columns alone don't disambiguate
    # TEAM-LESS futures rows (e.g. market_type="playoff_qualifier" is used
    # by both NFL and NBA, with neither nfl_game_id nor nba_game_id set for
    # either) -- default "nfl" keeps every pre-existing row correct with no
    # backfill needed.
    sport = Column(String, nullable=False, default="nfl")
    nfl_game_id = Column(String, ForeignKey("nfl_games.id"), nullable=True)
    nba_game_id = Column(String, ForeignKey("nba_games.id"), nullable=True)
    wnba_game_id = Column(String, ForeignKey("wnba_games.id"), nullable=True)
    cfb_game_id = Column(String, ForeignKey("cfb_games.id"), nullable=True)
    mlb_game_id = Column(String, ForeignKey("mlb_games.id"), nullable=True)
    mma_fight_id = Column(String, ForeignKey("mma_fights.id"), nullable=True)
    tennis_match_id = Column(Integer, ForeignKey("tennis_matches.id"), nullable=True)
    soccer_match_id = Column(Integer, ForeignKey("soccer_matches.id"), nullable=True)
    valorant_match_id = Column(Integer, ForeignKey("valorant_matches.id"), nullable=True)
    cs2_match_id = Column(Integer, ForeignKey("cs2_matches.id"), nullable=True)
    lol_match_id = Column(Integer, ForeignKey("lol_matches.id"), nullable=True)
    cod_match_id = Column(Integer, ForeignKey("cod_matches.id"), nullable=True)
    race_event_id = Column(Integer, ForeignKey("race_events.id"), nullable=True)  # motorsport (f1/irl/nascar)
    # moneyline | spread | total | division_winner | conference_champion |
    # one_seed | super_bowl_champion | playoff_qualifier -- the futures
    # kinds have no nfl_game_id/nba_game_id (season-long, not tied to one game)
    market_type = Column(String, nullable=False)
    source = Column(String, nullable=False)  # kalshi | polymarket
    source_event_id = Column(String, nullable=False)
    source_ticker = Column(String, nullable=False)
    team = Column(String, nullable=True)  # team the "yes" side favors (moneyline/spread/futures)
    line = Column(Float, nullable=True)  # spread/total line value
    # "over" | "under" for totals; Soccer's moneyline_3way also repurposes
    # this for the draw outcome ("home" | "draw" | "away", team is the home
    # team's name for the draw row since there's no team to favor there) --
    # same repurposed-field pattern as MLB's F5 "tie" side (see MlbMarketRow
    # in frontend/src/types/market.ts).
    side = Column(String, nullable=True)
    # Human-readable label for futures markets, which have no game to derive
    # a label from (e.g. "NFC West Division Winner") -- taken directly from
    # the source platform's own event title, no display logic needed.
    group_label = Column(String, nullable=True)
    # Soccer's correct_score market_type only (added 2026-07-19) -- the
    # real scoreline this row's YES side resolves on (e.g. 2-1). No other
    # sport/market_type in this app needs a two-integer outcome, so these
    # stay nullable/unused everywhere else, same sparse-field convention as
    # group_label (futures-only) and side's soccer-draw repurposing above.
    correct_score_home = Column(Integer, nullable=True)
    correct_score_away = Column(Integer, nullable=True)
    status = Column(String, nullable=False, default="active")
    # THE MARKET'S OWN RESOLUTION TERMS, straight from Kalshi. Kept because the
    # app previously stored a market's identifier and title but not what it
    # actually pays on, so questions like "does 15+ wins include playoffs?" and
    # "how is a tie settled?" could not be answered from the data at all -- and
    # the first of those flips a model-accuracy verdict completely depending on
    # the answer. Filled by app/ingestion/market_rules.py, not by the upsert
    # paths. BOTH fields matter: the edge cases (ties, voids) live in secondary.
    rules_primary = Column(String)
    rules_secondary = Column(String)
    rules_fetched_at = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class FuturesProbHistory(Base):
    """The MODEL's own probability for a futures leg, sampled over time.

    The market side of "how has this moved?" already exists -- MarketSnapshot
    records every poll. The model side did not exist anywhere: model_prob is
    computed on the READ path (each futures router prices its rows per request)
    and then thrown away, so there was no way to see whether the model changed
    its mind or only the market did.

    Kept separate from MarketSnapshot rather than adding a column to it: the
    pollers write snapshots every few minutes and know nothing about models,
    while this is sampled hourly off the priced endpoints. Merging them would
    mean writing a null model_prob on millions of rows to carry a value that
    changes far more slowly.
    """
    __tablename__ = "futures_prob_history"
    __table_args__ = (Index("ix_futures_prob_history_market_ts", "market_id", "ts"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    ts = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    model_prob = Column(Float, nullable=True)
    # The market price at the same instant, so a chart can be drawn from ONE
    # table without re-joining snapshots on an approximate timestamp.
    implied_prob = Column(Float, nullable=True)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    # Line-movement queries (2026-07-16) look up the snapshot closest to
    # "N hours ago" for a given market -- without this index that's a full
    # table scan per market row on a table that grows every 5 minutes for
    # every tracked market (thousands of rows/day).
    __table_args__ = (Index("ix_market_snapshots_market_ts", "market_id", "ts"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    ts = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    yes_bid = Column(Float, nullable=True)
    yes_ask = Column(Float, nullable=True)
    last_price = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)


class CatalogEntry(Base):
    """One series (Kalshi) or event (Polymarket) seen in a live catalog scan
    -- see app/ingestion/catalog_scan.py. Lets the app flag NFL markets that
    show up on either platform but aren't one of the market_types this app
    already knows how to ingest/model, so nothing tractable gets missed
    silently as new market types roll out over a season."""

    __tablename__ = "catalog_entries"
    __table_args__ = (UniqueConstraint("platform", "identifier", name="uq_platform_identifier"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String, nullable=False)  # kalshi | polymarket
    identifier = Column(String, nullable=False)  # Kalshi series ticker | Polymarket event slug
    title = Column(String, nullable=False)
    # nfl | nba | mlb | mma | tennis | soccer | valorant | cs2 | lol -- added
    # 2026-07-18 when catalog_scan.py grew beyond NFL-only -- plus "other",
    # the 2026-08-02 catch-all for live Kalshi Sports series belonging to no
    # tracked sport (see catalog_scan.py::fetch_kalshi_other_series).
    sport = Column(String, nullable=False, default="nfl")
    first_seen = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    last_seen = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    dismissed = Column(Integer, nullable=False, default=0)  # 0/1, sqlite has no bool
    disposition = Column(String, nullable=True)  # null (undecided) | "bootstrapped" | "flagged" -- set on dismiss, see catalog.py
    # WHY a free-text note and not just the disposition (added 2026-08-06, at
    # the user's request while triaging the backlog): "flagged" says an entry is
    # deferred but not WHY, so a deferred item is indistinguishable from one
    # nobody has looked at -- "I don't want to think they're just sitting
    # there". This records the actual blocker in the user's own terms, e.g.
    # "quoted but zero traded volume, revisit when volume appears" or "needs a
    # Serie C results source". It is decision provenance: when a scan
    # re-surfaces the same series months later, the reasoning is still attached
    # instead of being re-derived from scratch.
    note = Column(String, nullable=True)


class PlacedBet(Base):
    """A bet the user has actually placed on Kalshi/Polymarket, marked from
    the Recommended Bets list -- see app/api/routers/placed_bets.py. Stores
    a full SNAPSHOT of the market at placement time (not just a market_id
    reference) since prices/model_prob drift after placement and the point
    of this table is "what did I actually bet on, at what price, for how
    much" -- a live re-join against the current Market row would show the
    wrong numbers once the market moves.

    status starts "pending" and LOCKS stake_dollars out of its pool's
    portfolio-cap budget (see markets.ts::buildRecommendedBets) until it's
    settled won/lost/push/void. Game-tied markets (moneyline/spread/total/
    team_total) are auto-settled once the real final score lands (see
    app/models/bet_settlement.py) -- half-line markets and every
    futures/season-long market type have no such auto-settlement path (no
    final-score-equivalent data this app tracks), so those need a manual
    Settle action in the UI."""

    __tablename__ = "placed_bets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    market_type = Column(String, nullable=False)
    source = Column(String, nullable=False)
    sport = Column(String, nullable=False, default="nfl")  # see Market.sport
    team = Column(String, nullable=True)
    line = Column(Float, nullable=True)
    side = Column(String, nullable=True)
    label = Column(String, nullable=False)  # game_label or group_label at placement time
    nfl_game_id = Column(String, nullable=True)  # present only for game-tied market types
    nba_game_id = Column(String, nullable=True)  # present only for NBA game-tied market types
    wnba_game_id = Column(String, nullable=True)  # present only for WNBA game-tied market types
    cfb_game_id = Column(String, nullable=True)  # present only for CFB game-tied market types
    # Sub-league / competition, snapshotted at placement like every other field
    # here. "wnba" or "tennis" alone doesn't identify a row on the cross-sport
    # Bet Tracker -- TENNIS could be a Grand Slam or an ITF futures match, and
    # VALORANT a VCT international or a regional Challengers game. Nullable
    # because most sports are a single league and legitimately have none.
    league = Column(String, nullable=True)
    mlb_game_id = Column(String, nullable=True)  # present only for MLB game-tied market types
    mma_fight_id = Column(String, nullable=True)  # present only for MMA fight-tied market types
    tennis_match_id = Column(Integer, nullable=True)  # present only for Tennis match-tied market types
    soccer_match_id = Column(Integer, nullable=True)  # present only for Soccer match-tied market types
    valorant_match_id = Column(Integer, nullable=True)  # present only for Valorant match-tied market types
    cs2_match_id = Column(Integer, nullable=True)  # present only for CS2 match-tied market types
    lol_match_id = Column(Integer, nullable=True)  # present only for LoL match-tied market types
    cod_match_id = Column(Integer, nullable=True)  # present only for CoD match-tied market types
    race_event_id = Column(Integer, nullable=True)  # present only for motorsport (f1/irl/nascar) bets
    # WHICH SIDE OF THE CONTRACT: "yes" (buy the outcome) | "no" (buy against it).
    #
    # A SEPARATE COLUMN FROM `side` ON PURPOSE (#195, 2026-08-15). `side` above is
    # the OUTCOME SELECTOR and is already fully occupied -- measured across this
    # table: over 7343, set_1 605, yes 550, away 537, home 511, set_2 490, draw
    # 252, no 145, under 132, 2-0 88, kotko 46, submission 36, decision 33, tie 17.
    # Overloading it would erase the selector on an over/under row: a NO bet on
    # "Over 2.5" has to keep BOTH facts, "the Over rung" and "betting against it".
    # The 550 `yes` / 145 `no` values there are markets whose outcome literally IS
    # yes/no, which is a different thing again.
    #
    # Default "yes" is not a placeholder -- every row written before this column
    # existed is a YES bet by construction, because kelly_fraction refuses
    # negative edge and so the app could only ever surface YES. No backfill.
    #
    # SETTLEMENT MUST READ THIS. A NO bet wins exactly when the YES outcome
    # loses; grading one as a YES puts a wrong result in the tracker and a wrong
    # sign in every downstream ROI/CLV number. All five sites that assign
    # won/lost go through settlement.resolve_status_for_position().
    position = Column(String, nullable=False, default="yes")  # yes | no
    stake_pool = Column(String, nullable=False)  # "weekly" | "futures"
    stake_dollars = Column(Float, nullable=False)
    stake_units = Column(Float, nullable=True)
    market_prob_at_placement = Column(Float, nullable=True)
    model_prob_at_placement = Column(Float, nullable=True)
    edge_at_placement = Column(Float, nullable=True)
    placed_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    status = Column(String, nullable=False, default="pending")  # pending | won | lost | push | void
    settled_at = Column(DateTime, nullable=True)
    settlement_note = Column(String, nullable=True)
    # PAPER bets (auto-logged by paper_logger.py, no real money) vs REAL bets
    # the user marked placed. Paper bets exist ONLY to accrue forward CLV so we
    # can finally measure whether any (sport, market_type) bucket has real edge
    # -- so they feed compute_bet_clv + the CLV buckets exactly like real bets,
    # but are EXCLUDED from real-money views (/locked portfolio budget, /stats
    # ROI). Default False so every existing/real bet is unaffected.
    paper = Column(Boolean, nullable=False, default=False)
    # Was this row in the RECOMMENDED set at the moment it was logged?
    #
    # WHY IT MATTERS. The paper record is this app's only forward-validation
    # harness, and it deliberately logs BELOW the bet gate (PAPER_MIN_EDGE) to
    # gather measurement coverage. That is the right call -- but it means the
    # record is dominated by rows the app never recommended: measured
    # 2026-08-10, only 8 of 43 staked paper bets that day were in
    # compute_recommended's set. Any ROI, hit-rate or CLV computed over the
    # whole table therefore describes a portfolio that could never have been
    # held, which is precisely the number this harness exists to produce.
    #
    # NULL means "logged before this flag existed" -- deliberately tri-state so
    # old rows are not silently counted as either. Recorded at log time rather
    # than derived later, because the recommended set depends on pools, open
    # bets and prices as they were THEN and cannot be reconstructed afterwards.
    was_recommended = Column(Boolean, nullable=True)
    # The game/match start time as it stood WHEN THIS BET WAS PLACED. Compared
    # against the live-resolved start in /open to detect a reschedule (the game
    # moved to a later time/day) -- so the tracker can show the new date AND keep
    # it flagged as delayed/rescheduled. Nullable: legacy bets + markets with no
    # single start (auto-migrated additively, see _add_missing_columns).
    original_start_time = Column(String, nullable=True)  # ISO UTC


class RaceEvent(Base):
    """One motorsport race (F1 / IndyCar / NASCAR), keyed by its Kalshi event
    ticker. Exists so racing markets can live in the Market table like every
    other sport (and thus get price snapshots, paper-logging + CLV) -- start_time
    is the closing-line cutoff compute_bet_clv needs. Populated by poller_racing
    from the Kalshi markets' own close_time (a race-day proxy for the start)."""

    __tablename__ = "race_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    series = Column(String, nullable=False)          # f1 | irl | nascar
    event_ticker = Column(String, nullable=False, unique=True)
    name = Column(String, nullable=True)
    start_time = Column(DateTime, nullable=True)     # UTC; CLV closing-line cutoff
    status = Column(String, nullable=False, default="upcoming")
    # Final finishing result once the race is done (JSON string), populated by
    # the racing results scraper -> lets bet_settlement grade race markets.
    # Shape: {"order": [driver_id, ...] best->worst, "pole": driver_id|null}.
    result_json = Column(String, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class NewsAdjustmentCache(Base):
    """Cached result of the free, rule-based situational-factors pass for a
    game (injuries + rest/travel + weather -- see app/models/news_adjustment/).
    Recomputed automatically every poll cycle since it's free; POST
    /markets/{game_id}/refresh-news just forces an immediate recheck."""

    __tablename__ = "news_adjustment_cache"

    nfl_game_id = Column(String, ForeignKey("nfl_games.id"), primary_key=True)
    adjustment_pct = Column(Float, nullable=False)
    confidence = Column(String, nullable=False)
    factors_json = Column(String, nullable=False)  # JSON-encoded list of factor dicts
    requires_review = Column(Integer, nullable=False)  # 0/1, sqlite has no bool
    research_text = Column(String, nullable=True)
    computed_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    # Backup-QB-quality + injury-clustering only (see
    # injury_rules.py::offense_scoring_penalty_pp) -- a totals-space signal,
    # separate from adjustment_pct (which is win-probability-space and
    # includes every situational factor, not just these two).
    home_scoring_penalty_pp = Column(Float, nullable=True)
    away_scoring_penalty_pp = Column(Float, nullable=True)


class NbaNewsAdjustmentCache(Base):
    """Cached result of the free, rule-based NBA situational-factors pass
    (injuries + load-management -- see app/models/news_adjustment/
    situational_nba.py) -- parallel to NewsAdjustmentCache (NFL), own table
    since NFL's version uses nfl_game_id as its primary key."""

    __tablename__ = "nba_news_adjustment_cache"

    nba_game_id = Column(String, ForeignKey("nba_games.id"), primary_key=True)
    adjustment_pct = Column(Float, nullable=False)
    confidence = Column(String, nullable=False)
    factors_json = Column(String, nullable=False)
    requires_review = Column(Integer, nullable=False)
    computed_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)


class WnbaNewsAdjustmentCache(Base):
    """Cached result of the WNBA availability pass (injury_rules_wnba.py).

    Its own table for the same reason NBA's is: the primary key is this
    sport's own game id. WNBA carries injuries ONLY -- the rest/schedule-spot
    half that NBA has was measured for the WNBA and rejected (flat, wrong-
    signed slope over 1,467 games; see scripts/backtest_wnba_rest.py), so
    there is deliberately nothing here for it.
    """

    __tablename__ = "wnba_news_adjustment_cache"

    wnba_game_id = Column(String, ForeignKey("wnba_games.id"), primary_key=True)
    adjustment_pct = Column(Float, nullable=False)
    confidence = Column(String, nullable=False)
    factors_json = Column(String, nullable=False)
    requires_review = Column(Integer, nullable=False)
    computed_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)


class MlbNewsAdjustmentCache(Base):
    """Cached result of the free, rule-based MLB situational-factors pass
    (position-player injuries only so far -- see
    app/models/news_adjustment/situational_mlb.py) -- parallel to
    NbaNewsAdjustmentCache, own table since NFL's version uses nfl_game_id
    as its primary key."""

    __tablename__ = "mlb_news_adjustment_cache"

    mlb_game_id = Column(String, ForeignKey("mlb_games.id"), primary_key=True)
    adjustment_pct = Column(Float, nullable=False)
    confidence = Column(String, nullable=False)
    factors_json = Column(String, nullable=False)
    requires_review = Column(Integer, nullable=False)
    computed_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)


class SoccerNewsAdjustmentCache(Base):
    """Cached result of the free, rule-based Soccer injury-adjustment pass
    (Transfermarkt whole-league injury lists -- see
    app/models/news_adjustment/injury_rules_soccer.py) -- parallel to
    NbaNewsAdjustmentCache/MlbNewsAdjustmentCache, keyed by soccer_match_id
    since Soccer's own match FK is an int, not a string game id."""

    __tablename__ = "soccer_news_adjustment_cache"

    soccer_match_id = Column(Integer, ForeignKey("soccer_matches.id"), primary_key=True)
    adjustment_pct = Column(Float, nullable=False)
    confidence = Column(String, nullable=False)
    factors_json = Column(String, nullable=False)
    requires_review = Column(Integer, nullable=False)
    computed_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)


class NbaCoachSnapshot(Base):
    """Tracks each team's current head coach over time, refreshed every poll
    cycle -- needed because, unlike NFL (nflverse publishes coach names per
    historical game, so a change is a simple diff against an earlier row),
    ESPN's NBA roster endpoint only ever returns the CURRENT coach with no
    history. `since` is the first time THIS app observed the current
    coach_name for that team (not necessarily when the real hire happened,
    if it predates this app tracking it) -- coach_rules_nba.py uses recency
    of `since` as its "is this a genuinely recent, in-season change" signal,
    an honest approximation of NFL's more precise real-per-game data."""

    __tablename__ = "nba_coach_snapshot"

    team = Column(String, primary_key=True)
    coach_name = Column(String, nullable=False)
    since = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    season = Column(Integer, nullable=False)  # season the coach_name/since pair was recorded under
    # NULL on this app's very first-ever observation of a team (no real
    # history yet, `since` there just reflects "when this app started
    # tracking," not a real change) -- set to the prior coach_name only when
    # a GENUINE transition is detected. coach_rules_nba.py requires this to
    # be non-null before treating `since` as a real, recent change -- without
    # it, every team would look like a fresh coaching change for the first
    # ~45 days after this feature ships, since that's when every row is
    # first created.
    previous_coach_name = Column(String, nullable=True)


class ModelObservation(Base):
    """Every priced matchup, recorded BEFORE it happens, so the model can be
    scored against the market on OUTCOMES.

    WHY THIS EXISTS AND PlacedBet DOES NOT SUFFICE. This app can measure whether
    a model got more accurate; it cannot currently measure whether a model beats
    the MARKET. That question came up three separate times in one day (racing
    top_n, the MLB season sim, MMA style/defence features) and each time the
    answer was "we can't tell from here". Two structural reasons:

      1. PlacedBet only records matchups where the model ALREADY found an edge.
         Scoring on it measures the tail the app chose to bet, not calibration
         against the market -- it is guaranteed to flatter the model. Answering
         "do we beat the market" needs the UNSELECTED population, including all
         the boring rows where model and market agree.
      2. paper_logger was built to be judged on CLV, and this app later
         established that CLV does NOT predict profit. That retired the yardstick
         without replacing it.

    So: one row per priced market, logged whether or not it is bettable, scored
    on the real outcome. No stake, no bankroll, no effect on recommendations --
    this table is a measuring instrument, deliberately separate from PlacedBet so
    it can never pollute the tracker's P/L or the exposure caps.

    Gradeable fields deliberately MIRROR PlacedBet's names (team/side/line plus
    the per-sport entity ids) so bet_settlement's existing graders can grade an
    observation unchanged -- they read those attributes off whatever they are
    handed. Reusing the graders is what keeps this small; a parallel settlement
    path would be a second thing to keep correct.
    """

    __tablename__ = "model_observations"

    id = Column(Integer, primary_key=True)
    market_id = Column(Integer, index=True)
    sport = Column(String, index=True)
    market_type = Column(String, index=True)
    source = Column(String)

    # --- gradeable snapshot: names match PlacedBet so graders duck-type ------
    team = Column(String)
    side = Column(String)
    line = Column(Float)
    nfl_game_id = Column(String)
    nba_game_id = Column(String)
    wnba_game_id = Column(String)
    cfb_game_id = Column(String)
    mlb_game_id = Column(String)
    mma_fight_id = Column(String)
    tennis_match_id = Column(String)
    soccer_match_id = Column(String)
    valorant_match_id = Column(String)
    cs2_match_id = Column(String)
    lol_match_id = Column(String)
    cod_match_id = Column(String)
    race_event_id = Column(Integer)

    # --- what was believed, and what the market said, at observation time ----
    model_prob = Column(Float)
    market_prob = Column(Float)
    edge = Column(Float)
    volume = Column(Float)

    # First seen and last refreshed. The row is UPDATED while the event is still
    # upcoming (the model sharpens as team news and ratings move), then frozen
    # once it starts -- so `market_prob`/`model_prob` are the last pre-event
    # view, which is the honest thing to score. first_seen_at is kept so a
    # later analysis can ask how far out the observation was made.
    first_seen_at = Column(DateTime, index=True)
    observed_at = Column(DateTime, index=True)
    event_start = Column(DateTime, index=True)

    status = Column(String, default="pending", index=True)   # pending|won|lost|push|void
    settled_at = Column(DateTime)
    settlement_note = Column(String)
