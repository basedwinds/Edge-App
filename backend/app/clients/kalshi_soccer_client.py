"""Live-polling Kalshi client for Soccer moneyline (3-way Home/Draw/Away)
markets. Parallel to kalshi_tennis_client.py, but a genuinely different
market SHAPE: each match is THREE separate binary Yes/No markets sharing one
event_ticker (confirmed live 2026-07-19 via a real KXMLSGAME event: one
market per team + one "Tie" market, ticker suffixes "-{TEAM_CODE}"/"-TIE"),
not two markets naming each side the way NFL/Tennis moneyline does.

Home/away order: the shared event `title` ("San Jose vs Los Angeles G
Winner?" / "Liverpool vs Brentford Winner?") lists the HOME team FIRST --
confirmed live 2026-07-19 by cross-checking the real "Liverpool vs Brentford"
Kalshi title against football-data.co.uk's own HomeTeam="Liverpool" for that
exact real match (2026-05-24 E0 fixture). Relied on directly here rather than
re-derived per match.

Six GAME series confirmed live 2026-07-19 (see market_matcher_soccer.py's
_KALSHI_SOCCER_PREFIX_TO_DIVISION): KXEPLGAME/KXLALIGAGAME/KXSERIEAGAME/
KXBUNDESLIGAGAME/KXLIGUE1GAME/KXMLSGAME.

SPREAD and TOTAL series (KX{LEAGUE}SPREAD/KX{LEAGUE}TOTAL) confirmed live
2026-07-19 via a real KXMLSSPREAD/KXMLSTOTAL event -- both are real GOAL-count
LADDERS, same shape as this app's other sports' spread/total (see
kalshi_client.py's own get_spread_markets/get_total_markets docstrings):
  - SPREAD: one event per match ("San Jose vs Los Angeles G: Spread"), TWO
    markets per team per line (confirmed: 1.5 and 2.5 goals, 4 markets total
    per match), yes_sub_title is full text ("Los Angeles G wins by more than
    1.5 goals"), floor_strike gives the real threshold.
  - TOTAL: one event per match ("...: Total Goals"), one market per rung
    (confirmed: 0.5 through 5.5 goals, 6 rungs), yes_sub_title "Over X.5
    goals scored", team-less (game-level, not per-team)."""
import datetime
import re
import time

from app.clients.base import get_json, paginate

BASE = "https://api.elections.kalshi.com/trade-api/v2"

MONEYLINE_SERIES = {
    "E0": "KXEPLGAME",
    "SP1": "KXLALIGAGAME",
    "I1": "KXSERIEAGAME",
    "D1": "KXBUNDESLIGAGAME",
    "F1": "KXLIGUE1GAME",
    "MLS": "KXMLSGAME",
    "E1": "KXEFLCHAMPIONSHIPGAME",
    "P1": "KXLIGAPORTUGALGAME",
    "N1": "KXEREDIVISIEGAME",
    # Non-European leagues (2026-08-08), ratings sourced from football-data's
    # "extra" format -- see football_data_client.EXTRA_DIVISIONS. Verified live:
    # 63 / 48 / 39 / 36 open game markets respectively.
    "BRA1": "KXBRASILEIROGAME",
    "ARG1": "KXARGPREMDIVGAME",
    "MEX1": "KXLIGAMXGAME",
    "JPN1": "KXJLEAGUEGAME",
    # ---- Leagues this app ALREADY RATED but never listed markets for --------
    # Added 2026-08-08. These six needed no new data, no new model and no new
    # aliases -- their ratings have been in the pool the whole time. They were
    # added to support DOMESTIC CUPS and UEFA (a Coppa Italia tie needs Serie B,
    # a DFB Pokal tie needs 2. Bundesliga, UEFA needs Belgium/Turkey/Scotland),
    # and nobody went back to ask whether the leagues also traded in their own
    # right. They do: 297 open markets sitting unpriced behind a missing dict
    # entry.
    #
    # Found by diffing the RATED POOL against this map, which had never been
    # done -- ten rated leagues had no series here. This is the same drift as
    # the per-sport futures lists (loaders vs routes); the lesson generalises to
    # "diff the model's inputs against the ingester's inputs", not just routes.
    #
    # G1 (Greece), I2 (Serie B), F2 (Ligue 2) and E3 (League Two) are rated too
    # but had NO open series at this check -- their seasons have not started.
    # Deliberately not guessed at: a ticker that 404s is indistinguishable from
    # an off-season one, so they get added when they are observed live.
    "B1": "KXBELGIANPLGAME",
    "D2": "KXBUNDESLIGA2GAME",
    "E2": "KXEFLL1GAME",
    "SP2": "KXLALIGA2GAME",
    "T1": "KXSUPERLIGGAME",
    "SC0": "KXSCOTTISHPREMGAME",
    # ---- New countries, 2026-08-08 -----------------------------------------
    # Same extra-format pattern as BRA1/ARG1/MEX1/JPN1. Admitted only because
    # each has BOTH football-data history and a live ESPN feed -- Poland (66
    # open markets) and Switzerland (52) were rejected on the ESPN half, since
    # without it their bets could never settle. See EXTRA_DIVISIONS.
    "SWE1": "KXALLSVENSKANGAME",
    "NOR1": "KXELITESERIENGAME",
    "DNK1": "KXDENSUPERLIGAGAME",
    "CHN1": "KXCHNSLGAME",
    # ---- 2026-08-14, added on the rule the comment above set ----------------
    # F2 was deliberately left out on 2026-08-08 because its season had not
    # started and "a ticker that 404s is indistinguishable from an off-season
    # one, so they get added when they are observed live". It is now observed
    # live: KXLIGUE2GAME 42 open, all 42 two-sided, median spread 0.020.
    "F2": "KXLIGUE2GAME",
    # Saudi is a NEW league here, not a re-check. Rated since the ESPN wave-2
    # build; its settlement slug (ksa.1) was added to espn_soccer_client at the
    # same time as this entry, since without it the bets could never settle.
    # KXSAUDIPLGAME 33 open, all two-sided, median spread 0.030.
    "KSA1": "KXSAUDIPLGAME",
    # Serie B. Added on a REVERSED judgement, recorded because the reasoning
    # matters more than the entry: it was rejected hours earlier for a 0.110
    # median spread, ranking series by spread width. That screen was wrong for
    # this decision. Edge is measured against the MIDPOINT and entry is a limit
    # order whose quote you verify, so a wide book is not a cost you
    # automatically pay -- and a 5.5pp half-spread is not what decides anything
    # against a 10pp recommend threshold. Inventory that cannot clear that
    # threshold is inert: it prices or it does not, and it can never stake.
    # Spread is a JUNK FILTER for degenerate books (bid 0.02 / ask 0.97, no
    # volume, edge reading +1.000), not a quality ranking.
    # Live at this check: 3 open, all 3 two-sided, one listed match.
    "I2": "KXSERIEBGAME",
    # ---- 2026-08-18: the rated-pool diff, run again -------------------------
    # The 2026-08-08 note above said to re-run "diff the model's inputs against
    # the ingester's inputs" periodically. Doing so found SEVEN more leagues
    # whose ratings have been in the pool the whole time with no series entry.
    # Every one passed the same two-part admission test used for Saudi: a rated
    # pool AND a live ESPN slug, because without the slug the bets could never
    # settle. Volumes are total contracts traded across the series at the check.
    #
    #   COL1  KXDIMAYORGAME    54 mkts, all quoted, vol 8,167  (pool 6,218 team-matches)
    #   ECU1  KXECULPGAME      24 mkts, all quoted, vol 5,194  (pool 3,928)
    #   USL1  KXUSLGAME        39 mkts, all quoted, vol 4,921  (pool 6,634)
    #   NWSL  KXNWSLGAME       30 mkts, all quoted, vol 1,497  (pool 2,050)
    #   URY   KXURYPDGAME      24 mkts, all quoted, vol   304  (pool 4,420)
    #   VEN1  KXVENFUTVEGAME   42 mkts, all quoted, vol     0  (pool 3,938)
    #   G1    KXSLGREECEGAME   18 mkts, all quoted, vol     0  (pool 13,990)
    #
    # G1 IS NOT A NEW LEAGUE -- it is the one the note directly above gave up on
    # ("no series found under KXGREEKSL/KXGREECESL/KXSUPERLEAGUE"). The ticker is
    # KXSLGREECE: league-type first, country second, which is the opposite of
    # every other Kalshi soccer series. Found by sweeping /events?status=open and
    # reading the TITLES rather than guessing at tickers, which is the only
    # method that finds a series named against the pattern.
    #
    # VEN1 and G1 have ZERO traded volume -- listed, not traded. Wired anyway on
    # the DFB Pokal/EFL Cup precedent: an untraded market prices and is then
    # rejected by the staking gates, whereas leaving it out means discovering in
    # October that nothing was collected. Expect tracking rows, not bets.
    #
    # SWITZERLAND (SUI1) IS STILL REFUSED, and this is the second time. It has a
    # rated pool (2,616 team-matches) and a live KXSWISSLEAGUEGAME book, but
    # sui.1 returned ZERO events over a full month window -- the same ESPN half
    # of the test it failed on 2026-08-08. A market we can price but never settle
    # is worse than no market.
    # PERU and PARAGUAY, 2026-08-18 -- a SIDE EFFECT of completing CONMEBOL.
    # Both were rated only so Libertadores/Sudamericana ties involving their
    # clubs could price; having built the pools and the ESPN settlement slugs
    # (per.1, par.1), their own league markets cost nothing further.
    # KXPERLIGA1GAME 27 open markets (volume 7,681), KXAPFDDHGAME 18.
    #
    # Chile and Bolivia are NOT here: their pools exist for the same CONMEBOL
    # reason, but no Kalshi league series was found for either at this check.
    # They stay rating-only rather than being wired hopefully -- an entry that
    # never resolves is a silent per-pass fetch that always returns nothing.
    "PER1": "KXPERLIGA1GAME",
    "PAR1": "KXAPFDDHGAME",
    "COL1": "KXDIMAYORGAME",
    "ECU1": "KXECULPGAME",
    "URU1": "KXURYPDGAME",
    "VEN1": "KXVENFUTVEGAME",
    "USL1": "KXUSLGAME",
    "NWSL": "KXNWSLGAME",
    "G1": "KXSLGREECEGAME",
    # STILL ABSENT, re-checked 2026-08-18 and deliberately not guessed at:
    # E3 (KXEFLL2GAME 0 open). Stays rated-but-unwired -- that is zero
    # inventory, not a wide book, which is the one thing spread never told us.
}

SPREAD_SERIES = {
    "E0": "KXEPLSPREAD",
    "SP1": "KXLALIGASPREAD",
    "I1": "KXSERIEASPREAD",
    "D1": "KXBUNDESLIGASPREAD",
    "F1": "KXLIGUE1SPREAD",
    "MLS": "KXMLSSPREAD",
    "E1": "KXEFLCHAMPIONSHIPSPREAD",
    "P1": "KXLIGAPORTUGALSPREAD",
    "N1": "KXEREDIVISIESPREAD",
    # J-League has no spread/total series listed as of 2026-08-08.
    "BRA1": "KXBRASILEIROSPREAD",
    "ARG1": "KXARGPREMDIVSPREAD",
    "MEX1": "KXLIGAMXSPREAD",
    # Only the four with live spread inventory at the 2026-08-08 check. E2/SP2/T1
    # listed GAME markets but ZERO spread and ZERO total then, so they were absent
    # here rather than wired hopefully -- an entry that never resolves is a silent
    # per-pass fetch that always returns nothing.
    "B1": "KXBELGIANPLSPREAD",
    "D2": "KXBUNDESLIGA2SPREAD",
    "SC0": "KXSCOTTISHPREMSPREAD",
    "SWE1": "KXALLSVENSKANSPREAD",
    "NOR1": "KXELITESERIENSPREAD",
    "DNK1": "KXDENSUPERLIGASPREAD",
    "CHN1": "KXCHNSLSPREAD",
    # ---- 2026-08-14: E2/SP2/T1 re-checked and now HAVE inventory ------------
    # The note above is why they were missing, not a permanent verdict. Their
    # seasons have started and all three now quote two-sided:
    #   E2  KXEFLL1SPREAD    48 open, 48 quoted, median 0.040
    #   SP2 KXLALIGA2SPREAD   4 open,  4 quoted, median 0.015
    #   T1  KXSUPERLIGSPREAD  4 open,  4 quoted, median 0.010
    # plus the two new leagues wired in MONEYLINE_SERIES above:
    #   F2  KXLIGUE2SPREAD   36 open, 36 quoted, median 0.010
    #   KSA1 KXSAUDIPLSPREAD 24 open, 24 quoted, median 0.020
    #
    # A CAUTION worth recording: an earlier read the same afternoon put
    # KXEFLL1SPREAD at 0.110 and KXLIGUE2TOTAL at 0.090 over a comparable
    # sample, and the book tightened as kickoff approached. One spread snapshot
    # is not a stable property of a series -- these were wired on the later,
    # fuller read, and a series that looks wide hours out may not be.
    "E2": "KXEFLL1SPREAD",
    "SP2": "KXLALIGA2SPREAD",
    "T1": "KXSUPERLIGSPREAD",
    "F2": "KXLIGUE2SPREAD",
    "KSA1": "KXSAUDIPLSPREAD",
    # 2026-08-18, with the seven leagues added to MONEYLINE_SERIES above. Only
    # the three that actually list spread inventory -- ECU1/URU1/NWSL/G1 had
    # GAME markets and ZERO spread, so they are absent here rather than wired
    # hopefully, per the 2026-08-08 rule that an entry which never resolves is a
    # silent per-pass fetch that always returns nothing.
    # J-LEAGUE, 2026-08-18. The comment above SPREAD_SERIES' BRA1/ARG1/MEX1 block
    # said "J-League has no spread/total series listed as of 2026-08-08" -- true
    # then, stale now, and found by the catalog scan rather than by re-reading
    # the note. Probed live: KXJLEAGUESPREAD 8 open / 8 quoted, KXJLEAGUETOTAL
    # 12/12, KXJLEAGUEBTTS 2/2, all in the standard
    # "<team> wins by more than X goals" / "Over X goals scored" shapes.
    #
    # Volume is ZERO on all three, so these are listed-not-traded and will price
    # for tracking without producing bets -- same call as VEN1 and G1 earlier
    # today, on the DFB Pokal precedent: ingesting an untraded market costs a
    # fetch, and NOT ingesting it means noticing in October that nothing was
    # collected.
    "JPN1": "KXJLEAGUESPREAD",
    "COL1": "KXDIMAYORSPREAD",
    "VEN1": "KXVENFUTVESPREAD",
    "USL1": "KXUSLSPREAD",
}

LEAGUE_WINNER_SERIES = {
    "E0": ("KXPREMIERLEAGUE", "EPL Champion"),
    "SP1": ("KXLALIGA", "La Liga Champion"),
    "I1": ("KXSERIEA", "Serie A Champion"),
    "D1": ("KXBUNDESLIGA", "Bundesliga Champion"),
    "F1": ("KXLIGUE1", "Ligue 1 Champion"),
    # Added 2026-08-07. Both leagues gained GAME markets earlier the same day and
    # had no futures at all, which made them the two largest leagues in the app
    # with zero season-long coverage (P1 1,225 game markets, E1 735).
    #
    # INERT UNTIL KALSHI OPENS THE EVENTS: both series exist in the catalogue
    # (KXEFLCHAMPIONSHIP "EFL Championship League Winner", KXLIGAPORTUGAL "Liga
    # Portugal Winner") but returned 0 open events when this was wired, while
    # KXPREMIERLEAGUE already had its 2027 event open. Wired now anyway because
    # the change is one dict entry and the alternative is noticing weeks late --
    # same posture as the CFB spread ingestion, which sat inert by design until
    # Kalshi listed it.
    #
    # No new model needed: simulate_season is league-agnostic (it builds its own
    # double round-robin rather than reading a fixture list) and takes its team
    # list from the ingested markets themselves, so both price the moment rows
    # exist. The group_label below is OURS, not Kalshi's title -- the router's
    # _MARKET_TYPE_LABEL_TO_DIVISION is derived from this same dict, so the two
    # cannot disagree.
    "E1": ("KXEFLCHAMPIONSHIP", "EFL Championship Winner"),
    "P1": ("KXLIGAPORTUGAL", "Liga Portugal Champion"),
    "N1": ("KXEREDIVISIE", "Eredivisie Champion"),
    # Added 2026-08-09, and ONLY these two of the three leagues that have live
    # winner futures. Both are straight double round-robins decided on the final
    # table, which is exactly what simulate_season models.
    #
    # They are also both MID-SEASON (Brazil 20.5 of 38 rounds, China 20.5 of
    # 30), which is why they could not be wired until simulate_season learned to
    # start from the real table -- before that it would have simulated a fresh
    # season and thrown away an 8-point lead.
    "BRA1": ("KXBRASILEIRO", "Brasileirao Champion"),
    "CHN1": ("KXCHNSL", "Chinese Super League Champion"),
    # DELIBERATELY NOT WIRED: Liga MX (KXLIGAMX, 36 open markets, the biggest of
    # the three). Its markets ask "Will <team> win the Liga MX Clausura?" and
    # the Clausura is decided by the LIGUILLA -- an 8-team knockout playoff --
    # not by the regular-season table. simulate_season would answer a different
    # question than the one being traded, and answer it confidently. This is the
    # same reason MLS Cup is excluded (see _MLS_PLAYOFF_MARKET_TYPES); pricing
    # it needs a bracket model, not a table model.
}
# A dedicated "KXEPLTOP4"-style series per league (KXEPLTOP4, KXLALIGATOP4,
# etc) exists but had ZERO open events on Kalshi as of 2026-07-19 --
# confirmed real, just not this app's actual EPL Top-4 source (see
# TOP_N_SERIES below: EPL's REAL, live Top-4/Top-2/Top-Half futures live
# under a DIFFERENT series, "KXEPLTOP", found during a later 2026-07-19
# catalog_scan.py audit -- this empty per-league series was a real dead end,
# not the same market re-discovered). MLS has no league_winner-shaped market
# either -- KXMLSCUP is a PLAYOFF bracket, not a table finish, a genuinely
# different real structure the round-robin season model doesn't cover. That is
# still why MLS is absent from LEAGUE_WINNER_SERIES above; it is now modelled
# separately by playoff_sim_mls.py and ingested via MLS_PLAYOFF_SERIES below.

TOTAL_SERIES = {
    "E0": "KXEPLTOTAL",
    "SP1": "KXLALIGATOTAL",
    "I1": "KXSERIEATOTAL",
    "D1": "KXBUNDESLIGATOTAL",
    "F1": "KXLIGUE1TOTAL",
    "MLS": "KXMLSTOTAL",
    "E1": "KXEFLCHAMPIONSHIPTOTAL",
    "P1": "KXLIGAPORTUGALTOTAL",
    "N1": "KXEREDIVISIETOTAL",
    "BRA1": "KXBRASILEIROTOTAL",
    "ARG1": "KXARGPREMDIVTOTAL",
    "MEX1": "KXLIGAMXTOTAL",
    "B1": "KXBELGIANPLTOTAL",
    "D2": "KXBUNDESLIGA2TOTAL",
    "SC0": "KXSCOTTISHPREMTOTAL",
    "SWE1": "KXALLSVENSKANTOTAL",
    "NOR1": "KXELITESERIENTOTAL",
    "DNK1": "KXDENSUPERLIGATOTAL",
    "CHN1": "KXCHNSLTOTAL",
    # 2026-08-14, same live re-check as SPREAD_SERIES above (see that comment
    # for the "one snapshot is not a stable property" caution):
    #   E2  KXEFLL1TOTAL     72 open, 72 quoted, median 0.040
    #   SP2 KXLALIGA2TOTAL    6 open,  6 quoted, median 0.020
    #   T1  KXSUPERLIGTOTAL   6 open,  6 quoted, median 0.010
    #   F2  KXLIGUE2TOTAL    54 open, 54 quoted, median 0.020
    #   KSA1 KXSAUDIPLTOTAL  36 open, 36 quoted, median 0.020
    "E2": "KXEFLL1TOTAL",
    "SP2": "KXLALIGA2TOTAL",
    "T1": "KXSUPERLIGTOTAL",
    "F2": "KXLIGUE2TOTAL",
    "KSA1": "KXSAUDIPLTOTAL",
    # 2026-08-18, same three leagues and same reasoning as SPREAD_SERIES above.
    "JPN1": "KXJLEAGUETOTAL",   # see the JPN1 note in SPREAD_SERIES
    "COL1": "KXDIMAYORTOTAL",
    "VEN1": "KXVENFUTVETOTAL",
    "USL1": "KXUSLTOTAL",
}

# BTTS (Both Teams To Score) confirmed live 2026-07-19 with real open
# inventory for MLS ONLY (KXMLSBTTS, 30 open events, real per-match) --
# the 5 European leagues' own BTTS series (KXEPLBTTS etc) exist but had
# ZERO open events at the same check (off-season, same pattern as their own
# GAME/SPREAD/TOTAL series before the season starts) -- built for all 6 keys
# anyway so European coverage activates automatically once real events open,
# same "the code doesn't need to change, just the live inventory" precedent
# as every other market type here.
BTTS_SERIES = {
    "E0": "KXEPLBTTS",
    "SP1": "KXLALIGABTTS",
    "I1": "KXSERIEABTTS",
    "D1": "KXBUNDESLIGABTTS",
    "F1": "KXLIGUE1BTTS",
    "MLS": "KXMLSBTTS",
    "E1": "KXEFLCHAMPIONSHIPBTTS",
    "P1": "KXLIGAPORTUGALBTTS",
    "N1": "KXEREDIVISIEBTTS",
    # 2026-08-18. These three DO have live BTTS inventory, unlike the five
    # European series above which were wired ahead of their seasons.
    "JPN1": "KXJLEAGUEBTTS",    # see the JPN1 note in SPREAD_SERIES
    "COL1": "KXDIMAYORBTTS",
    "VEN1": "KXVENFUTVEBTTS",
    "USL1": "KXUSLBTTS",
}

# Relegation confirmed live 2026-07-19 for all 5 European leagues
# (KXEPLRELEGATION-27 etc, real per-team "is this team relegated Y/N"
# markets, 20/18 real markets per league) -- see season_sim_soccer.py's own
# docstring on why Bundesliga/Ligue 1's real playoff mechanics make this
# app's own model a LOWER BOUND for the team right at that boundary. No MLS
# entry -- MLS has no relegation.
RELEGATION_SERIES = {
    "E0": ("KXEPLRELEGATION", "EPL Relegation"),
    "SP1": ("KXLALIGARELEGATION", "La Liga Relegation"),
    "I1": ("KXSERIEARELEGATION", "Serie A Relegation"),
    "D1": ("KXBUNDESLIGARELEGATION", "Bundesliga Relegation"),
    "F1": ("KXLIGUE1RELEGATION", "Ligue 1 Relegation"),
}


# EPL-only real inventory (confirmed live 2026-07-19): ONE series
# ("KXEPLTOP") holds THREE real event tickers at once -- "-27TOPHALF"
# (Top Half Finishers), "-27TOP4" (Top 4 -- the REAL Champions-League-
# qualification-style futures this app's earlier live audit had marked as
# "confirmed to exist, zero real open events", checked against the WRONG
# series ticker "KXEPLTOP4" -- the real market lives here instead, under
# "KXEPLTOP", a genuinely different real discovery, not the same finding
# re-confirmed), and "-27TOP2" (Top 2). Not found for the other 4 leagues
# during the same live scan -- EPL-only for now, same "ship what has real
# inventory" precedent as everything else in this app.
TOP_N_SERIES = {"E0": "KXEPLTOP"}

# Season points ladders: "Will <team> finish the 2026-27 season with N+ points?".
# Unlike every other futures series here, these are a LADDER -- one market per
# (team, threshold) rather than one per team -- so a row needs both the team and
# the number. Confirmed live 2026-08-02: 384 open markets across the five
# leagues, each under a SINGLE event per league, yes_sub_title of the form
# "Tottenham: 75+ Points", and the threshold carried in floor_strike as N-0.5
# (74.5 for a 75+ market). All five were unquoted at the time -- the 2026-27
# seasons had not kicked off -- which is expected, not a fault.
TEAM_POINTS_SERIES = {
    "E0": "KXEPLTEAMPOINTS",
    "SP1": "KXLALIGATEAMPOINTS",
    "I1": "KXSERIEATEAMPOINTS",
    "D1": "KXBUNDESLIGATEAMPOINTS",
    "F1": "KXLIGUE1TEAMPOINTS",
}
_TOP_N_EVENT_LABELS = {"TOPHALF": ("top_half", "EPL Top Half"), "TOP4": ("top4", "EPL Top 4"), "TOP2": ("top2", "EPL Top 2")}


def get_open_events(series_ticker: str) -> list[dict]:
    def url_builder(cursor):
        url = f"{BASE}/events?series_ticker={series_ticker}&status=open&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        return url

    return paginate(url_builder, list_key="events", cursor_style="cursor")


# --- BATCHED MARKET FETCH (2026-08-08) -------------------------------------
# MEASURED PROBLEM this solves. run_full_refresh_soccer was taking 784s against
# a 300s interval, with "kalshi markets" alone at 415s -- 1.4x the entire
# interval -- and that figure swung 174s -> 415s between consecutive passes,
# which is rate-limit backoff variance rather than workload. The app had logged
# 805 Kalshi 429s since startup; base.get_json sleeps 2*(attempt+1)s per 429 up
# to four retries, so a throttled call can burn 12s doing nothing, and every
# sport's poller competes for the same quota.
#
# THE CAUSE was one HTTP call PER EVENT. Each fetcher below walks
# get_open_events(series) and then calls get_markets_for_event() for every
# event it found -- hundreds of round trips per cycle across 9 leagues and a
# dozen market types. But Kalshi will return every market for a whole SERIES in
# a single request, which is how this session's probes read all 441 UEFA
# markets instantly. So the per-event call is replaced by one batched fetch per
# series, memoized for a short window and grouped by event_ticker.
#
# NO CALL SITE CHANGES. All 22 callers keep calling get_markets_for_event; it
# just answers from the batch now. The series is recoverable from the event
# ticker, which is always "{SERIES}-{EVENT SUFFIX}".
#
# FALLS BACK RATHER THAN GUESSING: if an event is absent from its series batch
# (an unexpected ticker shape, or a market the series query does not surface),
# the original per-event request is issued for that event alone. A miss costs
# one call, never a wrong or empty answer.
_MARKET_BATCH_TTL_SECONDS = 120  # shorter than the 300s poll interval, so each cycle refetches once
_market_batch_cache: dict[str, tuple[float, dict[str, list[dict]]]] = {}


def _series_of(event_ticker: str) -> str | None:
    head = (event_ticker or "").split("-", 1)[0].strip()
    return head or None


def _markets_by_event_for_series(series_ticker: str) -> dict[str, list[dict]]:
    cached = _market_batch_cache.get(series_ticker)
    if cached and (time.time() - cached[0]) < _MARKET_BATCH_TTL_SECONDS:
        return cached[1]

    def url_builder(cursor):
        url = f"{BASE}/markets?series_ticker={series_ticker}&status=open&limit=1000"
        if cursor:
            url += f"&cursor={cursor}"
        return url

    try:
        markets = paginate(url_builder, list_key="markets", cursor_style="cursor")
    except Exception:
        # A failed batch must not poison the cache -- leave it unset so the
        # per-event fallback handles this cycle and the next pass retries.
        return {}
    grouped: dict[str, list[dict]] = {}
    for m in markets:
        ev = m.get("event_ticker")
        if ev:
            grouped.setdefault(ev, []).append(m)
    _market_batch_cache[series_ticker] = (time.time(), grouped)
    return grouped


def get_markets_for_event(event_ticker: str) -> list[dict]:
    series = _series_of(event_ticker)
    if series:
        grouped = _markets_by_event_for_series(series)
        hit = grouped.get(event_ticker)
        if hit is not None:
            return hit
    d = get_json(f"{BASE}/markets?event_ticker={event_ticker}")
    return d.get("markets", [])


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Kalshi's soccer occurrence_datetime is NOT the kickoff. It is the point the
# market stops being about a live event, which for soccer sits a flat 3 hours
# after the first whistle -- 90 minutes of play, half time, stoppage and the
# post-match settlement window. Assigning it straight to estimated_start_time
# put every Kalshi-priced fixture 3 hours in the future, so a match already at
# half time still read as "upcoming" and stayed on the recommended board.
#
# MEASURED 2026-08-14, not assumed. Every soccer fixture stored for 2026-08-13
# to 08-16 was matched BY TEAM NAME against ESPN's real kickoff for the same
# league:
#
#     start_time_source   n        offset vs ESPN
#     espn              125/125    +0.00h   (control: that path is correct)
#     platform           16/17     +3.00h   (this defect, a clean constant)
#                         1/17    +27.00h   (+3h plus a day: a reschedule whose
#                                            occurrence was never revised)
#
# A first pass matched fixtures by NEAREST KICKOFF in the same league instead of
# by team, and reported a scattered +0.5/+0.75/+1.0/+2.25h spread that made this
# look like noise rather than a constant -- fixtures with no ESPN counterpart
# glom onto a neighbouring match and manufacture a fake offset. The ESPN control
# row is what proves the join sound: 125 of 125 at exactly zero.
#
# THE CORRECTION BELONGS HERE, NOT IN THE SHARED WRITER. Polymarket's soccer
# client fills the same estimated_start_time field from gameStartTime, which is
# a REAL kickoff, and both venues are tagged "platform" downstream -- so a blanket
# -3h in market_catalog_soccer would have silently moved every Polymarket fixture
# 3 hours early. This is the only place that knows the value is an occurrence.
OCCURRENCE_TO_KICKOFF_HOURS = 3


def _kickoff_from_occurrence(occurrence: str | None) -> str | None:
    """Real kickoff from Kalshi's occurrence_datetime, or None if unparseable.

    Returns None rather than the raw value on a parse failure: a MISSING start
    time degrades to match_date and lets the ESPN poller fill it in, whereas a
    3-hours-wrong one actively recommends matches that are already being played.
    """
    if not occurrence:
        return None
    try:
        t = datetime.datetime.strptime(str(occurrence), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        try:
            t = datetime.datetime.strptime(str(occurrence), "%Y-%m-%dT%H:%MZ")
        except (TypeError, ValueError):
            return None
    t -= datetime.timedelta(hours=OCCURRENCE_TO_KICKOFF_HOURS)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


_TITLE_SUFFIX_RE = re.compile(
    r"\s+Winner\?$|:\s*Spread$|:\s*Total Goals$"
    r"|:\s*First Half Winner$|:\s*First Half Spread$|:\s*First Half Total$|:\s*First Half BTTS$"
    r"|:\s*Second Half Winner$|:\s*Second Half Spread$|:\s*Second Half Total$|:\s*Second Half BTTS$"
    r"|:\s*First Team to Score$|:\s*Correct Score$|:\s*Team Total$"
)


def _parse_title_teams(title: str) -> tuple[str, str] | None:
    """"Liverpool vs Brentford Winner?" -> ("Liverpool", "Brentford"), home
    first (see module docstring). Also handles the SPREAD/TOTAL series' own
    title suffixes ("...: Spread" / "...: Total Goals") -- same "home team
    listed first" convention confirmed for the GAME series applies here too
    (all three series share the same underlying match, same event-creation
    pipeline on Kalshi's side)."""
    if " vs " not in title:
        return None
    left, _, right = title.partition(" vs ")
    right = _TITLE_SUFFIX_RE.sub("", right).strip()
    left = left.strip()
    if not left or not right:
        return None
    return left, right


def get_moneyline_markets() -> list[dict]:
    """One row per (event, outcome) across all 6 leagues -- 3 rows per real
    match (home/away/draw), each a plain binary Yes/No market. `side` is
    "home"/"away"/"draw", resolved by comparing yes_sub_title against the
    event title's own home/away team names (ticker suffix alone isn't
    reliably a team abbreviation for every league -- "Tie" is always
    unambiguous, but team-side tickers use ad-hoc per-league codes, e.g.
    "-LAG"/"-SJ" -- text comparison against the title is more robust)."""
    rows = []
    for division, series_ticker in MONEYLINE_SERIES.items():
        events = get_open_events(series_ticker)
        for ev in events:
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                label = m.get("yes_sub_title", "")
                if label.lower() == "tie":
                    side, team = "draw", None
                elif label == home_team:
                    side, team = "home", home_team
                elif label == away_team:
                    side, team = "away", away_team
                else:
                    continue  # label didn't match either known team or "Tie" -- skip rather than guess
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "event_title": title,
                    "division": division,
                    "home_team": home_team,
                    "away_team": away_team,
                    "ticker": m["ticker"],
                    "side": side,
                    "team": team,
                    "estimated_start_time": _kickoff_from_occurrence(m.get("occurrence_datetime")),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


_SPREAD_SUB_RE = re.compile(r"^(.+?) wins by more than [\d.]+ goals$")


def get_spread_markets() -> list[dict]:
    """One row per (event, team, line) -- 4 rows per real match (2 teams x
    2 lines, see module docstring). `team` is resolved by comparing the
    parsed name inside yes_sub_title against the event's known home/away
    teams, same "text comparison, not ticker-suffix" reasoning as moneyline."""
    rows = []
    for division, series_ticker in SPREAD_SERIES.items():
        events = get_open_events(series_ticker)
        for ev in events:
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub_match = _SPREAD_SUB_RE.match(m.get("yes_sub_title", ""))
                if not sub_match:
                    continue
                name = sub_match.group(1)
                if name == home_team:
                    team = home_team
                elif name == away_team:
                    team = away_team
                else:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": division,
                    "home_team": home_team,
                    "away_team": away_team,
                    "ticker": m["ticker"],
                    "team": team,
                    "line": _to_float(m.get("floor_strike")),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


_TOTAL_SUB_RE = re.compile(r"^Over ([\d.]+) goals scored$")


def get_total_markets() -> list[dict]:
    """One row per (event, line) -- game-level, team-less ladder (6 rungs
    confirmed live, see module docstring)."""
    rows = []
    for division, series_ticker in TOTAL_SERIES.items():
        events = get_open_events(series_ticker)
        for ev in events:
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub_match = _TOTAL_SUB_RE.match(m.get("yes_sub_title", ""))
                if not sub_match:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": division,
                    "home_team": home_team,
                    "away_team": away_team,
                    "ticker": m["ticker"],
                    "line": float(sub_match.group(1)),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


_BTTS_TITLE_SUFFIX_RE = re.compile(r":\s*BTTS$")


def get_btts_markets() -> list[dict]:
    """One row per real match -- BTTS is a single binary Yes/No market
    (event-level, no per-team/per-line split like moneyline/spread), so
    unlike get_moneyline_markets there's no label-matching loop: whichever
    single market exists under the event IS the BTTS market (confirmed live
    2026-07-19 against a real KXMLSBTTS event -- exactly one market per
    event, yes_sub_title empty/generic, the event title itself carries the
    match). Title suffix is "...: BTTS" (confirmed live -- NOT the spelled-
    out "Both Teams To Score" every other series' title suffix pattern here
    would suggest), stripped the same way SPREAD/TOTAL strip their own
    suffix before team-name parsing."""
    rows = []
    for division, series_ticker in BTTS_SERIES.items():
        events = get_open_events(series_ticker)
        for ev in events:
            raw_title = ev.get("title", "")
            title = _BTTS_TITLE_SUFFIX_RE.sub("", raw_title).strip()
            teams = _parse_title_teams(title if " vs " in title else raw_title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": division,
                    "home_team": home_team,
                    "away_team": away_team,
                    "ticker": m["ticker"],
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_relegation_markets() -> list[dict]:
    """One row per (league, team-in-the-field) -- same real shape as
    get_league_winner_markets (single event per league/season, one binary
    market per team, no title-parsing needed), confirmed live 2026-07-19
    against a real KXEPLRELEGATION-27 event (20 real per-team markets,
    ticker suffix a team code, yes_sub_title the real full team name --
    "Yes" resolves to THAT team being relegated)."""
    rows = []
    for division, (series_ticker, group_label) in RELEGATION_SERIES.items():
        for ev in get_open_events(series_ticker):
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                team = m.get("yes_sub_title", "")
                if not team:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": division,
                    "group_label": group_label,
                    "ticker": m["ticker"],
                    "team": team,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_team_points_markets() -> list[dict]:
    """One row per (league, team, points threshold) for the KX*TEAMPOINTS ladders.

    Two things differ from the other per-team futures fetchers above. The team
    name has to be split off yes_sub_title ("Tottenham: 75+ Points"), since the
    same team appears on several rungs. And the threshold is taken from
    floor_strike rather than parsed out of the title -- Kalshi already states it
    numerically, and reading "75+" out of prose would break the moment a market
    is worded differently. A row with no usable team or no floor_strike is
    SKIPPED rather than guessed at: an unpriceable rung is better than a rung
    priced against the wrong number."""
    rows = []
    for division, series_ticker in TEAM_POINTS_SERIES.items():
        for ev in get_open_events(series_ticker):
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub = m.get("yes_sub_title") or ""
                team = sub.split(":", 1)[0].strip()
                floor = m.get("floor_strike")
                if not team or floor is None:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": division,
                    "ticker": m["ticker"],
                    "team": team,
                    "line": _to_float(floor),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_league_winner_markets() -> list[dict]:
    """One row per (league, team-in-the-field) -- confirmed live 2026-07-19
    via a real KXPREMIERLEAGUE-27 event: 20 real per-team markets (real
    volume, e.g. Arsenal $8.1k, Man City $11.2k), ticker suffix a team code
    (e.g. "-ARS"), yes_sub_title the real full team name. Single event per
    league (one season's worth of teams), not per-match like GAME/SPREAD/
    TOTAL -- no team-name title-parsing needed here."""
    rows = []
    for division, (series_ticker, group_label) in LEAGUE_WINNER_SERIES.items():
        for ev in get_open_events(series_ticker):
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                team = m.get("yes_sub_title", "")
                if not team:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": division,
                    "group_label": group_label,
                    "ticker": m["ticker"],
                    "team": team,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


# MLS Cup Playoffs (added 2026-08-07). Priced by playoff_sim_mls.py, NOT by the
# round-robin season model that handles the European league_winner markets --
# see that module's docstring for why MLS needs its own.
#
# ONE simulation prices all three of these series, which is why they are grouped
# in a single dict: KXMLSEAST/KXMLSWEST resolve on winning the CONFERENCE
# BRACKET, not on topping the regular-season conference table. That was checked
# against Kalshi's own rules_primary rather than inferred from the series name
# -- KXMLSEAST-26-TOR reads "...is the 2026 MLS Eastern Conference champion",
# and the East/West bracket winners are exactly the two teams the sim already
# has to produce on the way to an MLS Cup winner. Pricing them off the
# regular-season table instead would answer a different question (Vancouver
# leading the West in August is not the same proposition as Vancouver winning
# the Western bracket in December).
#
# Live inventory confirmed 2026-08-07: KXMLSCUP 30 open, KXMLSEAST 15,
# KXMLSWEST 15 -- one market per team, the same one-event-per-series shape as
# LEAGUE_WINNER_SERIES, so no title parsing.
#
# NOT included: KXMLSLEADER (17 open). That is the golden boot ("Will <player>
# lead MLS in goals"), a PLAYER season-stat market -- the family this app
# already measured and put in PLAYER_STAT_TRACKING_ONLY. Different question,
# different model, deliberately out of scope here.
MLS_PLAYOFF_SERIES = {
    "KXMLSCUP": ("mls_cup_winner", "MLS Cup"),
    "KXMLSEAST": ("mls_conference_winner", "MLS Eastern Conference"),
    "KXMLSWEST": ("mls_conference_winner", "MLS Western Conference"),
}


# Liga MX, added 2026-08-11. Same one-market-per-team shape as the MLS series
# above, but note it carries TWO open events at once -- KXLIGAMX-27APER and
# KXLIGAMX-27CLA -- because Liga MX plays two full torneos a year and Kalshi
# lists both. They are separate championships, so the event ticker (not just the
# series) has to reach the group_label or the two would collapse into one
# 36-team field that never plays.
#
# It sat out of LEAGUE_WINNER_SERIES on purpose until now: a torneo is won in
# the LIGUILLA knockout, not on the table, so season_sim_soccer would have
# answered a different question. playoff_sim_ligamx.py is the model that answers
# the traded one.
LIGAMX_SERIES = "KXLIGAMX"


def _ligamx_group_label(event_ticker: str) -> str:
    """"KXLIGAMX-27CLA" -> "Liga MX Clausura". The suffix is what separates the
    two torneos, and getting it wrong merges two distinct championships."""
    suffix = (event_ticker or "").rsplit("-", 1)[-1].upper()
    if suffix.endswith("CLA"):
        return "Liga MX Clausura"
    if suffix.endswith("APER"):
        return "Liga MX Apertura"
    return f"Liga MX {suffix}"


def get_ligamx_markets() -> list[dict]:
    """One row per (torneo, team). yes_sub_title is the team name."""
    rows = []
    for ev in get_open_events(LIGAMX_SERIES):
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        label = _ligamx_group_label(ev["event_ticker"])
        for m in markets:
            team = m.get("yes_sub_title", "")
            if not team:
                continue
            rows.append({
                "event_ticker": ev["event_ticker"],
                "division": "MEX1",
                "market_type": "ligamx_champion",
                "group_label": label,
                "ticker": m["ticker"],
                "team": team,
                "yes_bid": _to_float(m.get("yes_bid_dollars")),
                "yes_ask": _to_float(m.get("yes_ask_dollars")),
                "last_price": _to_float(m.get("last_price_dollars")),
                "volume": _to_float(m.get("volume_fp")),
                "status": m.get("status"),
            })
    return rows


def get_mls_playoff_markets() -> list[dict]:
    """One row per (series, team). Same per-team shape as
    get_league_winner_markets -- yes_sub_title is the full team name."""
    rows = []
    for series_ticker, (market_type, group_label) in MLS_PLAYOFF_SERIES.items():
        for ev in get_open_events(series_ticker):
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                team = m.get("yes_sub_title", "")
                if not team:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": "MLS",
                    "market_type": market_type,
                    "group_label": group_label,
                    "ticker": m["ticker"],
                    "team": team,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


# ---------------------------------------------------------------------------
# Second batch (added 2026-07-19, same day, after a full catalog_scan.py
# audit surfaced real, live inventory this app hadn't covered yet): First
# Half / Second Half / First Team To Score / Correct Score / Team Total.
# Confirmed live via real KXMLS* events (MLS is the only currently in-season
# league -- the 5 European leagues' own equivalents return the identical
# empty pattern every other per-match series shows right now, see module
# docstring). Built for all leagues with a real confirmed series (not just
# MLS) so European coverage activates automatically once their season
# starts, same "ship the code, thin/empty until in-season" precedent as the
# original GAME/SPREAD/TOTAL series.
# ---------------------------------------------------------------------------

FIRST_HALF_SERIES = {
    "E0": "KXEPL1H", "SP1": "KXLALIGA1H", "I1": "KXSERIEA1H",
    "D1": "KXBUNDESLIGA1H", "F1": "KXLIGUE11H", "MLS": "KXMLS1H",
    "E1": "KXEFLCHAMPIONSHIP1H",
    "P1": "KXLIGAPORTUGAL1H",
    "N1": "KXEREDIVISIE1H",
}
FIRST_HALF_SPREAD_SERIES = {
    "E0": "KXEPL1HSPREAD", "SP1": "KXLALIGA1HSPREAD", "I1": "KXSERIEA1HSPREAD",
    "D1": "KXBUNDESLIGA1HSPREAD", "F1": "KXLIGUE11HSPREAD", "MLS": "KXMLS1HSPREAD",
    "E1": "KXEFLCHAMPIONSHIP1HSPREAD",
    "P1": "KXLIGAPORTUGAL1HSPREAD",
    "N1": "KXEREDIVISIE1HSPREAD",
}
FIRST_HALF_TOTAL_SERIES = {
    "E0": "KXEPL1HTOTAL", "SP1": "KXLALIGA1HTOTAL", "I1": "KXSERIEA1HTOTAL",
    "D1": "KXBUNDESLIGA1HTOTAL", "F1": "KXLIGUE11HTOTAL", "MLS": "KXMLS1HTOTAL",
    "E1": "KXEFLCHAMPIONSHIP1HTOTAL",
    "P1": "KXLIGAPORTUGAL1HTOTAL",
    "N1": "KXEREDIVISIE1HTOTAL",
}
FIRST_HALF_BTTS_SERIES = {
    "E0": "KXEPL1HBTTS", "SP1": "KXLALIGA1HBTTS", "I1": "KXSERIEA1HBTTS",
    "D1": "KXBUNDESLIGA1HBTTS", "F1": "KXLIGUE11HBTTS", "MLS": "KXMLS1HBTTS",
    "E1": "KXEFLCHAMPIONSHIP1HBTTS",
    "P1": "KXLIGAPORTUGAL1HBTTS",
    "N1": "KXEREDIVISIE1HBTTS",
}

# Second Half confirmed live ONLY for EPL/La Liga on Kalshi (catalog_scan.py
# found no KX{LEAGUE}2H* series for Bundesliga/Ligue1/Serie A/MLS at all --
# a real, confirmed platform gap, not an oversight here) -- Polymarket DOES
# have a real "second-half-result" market for MLS (see
# polymarket_soccer_client.py), so Second Half coverage genuinely differs by
# platform, not just by season.
SECOND_HALF_SERIES = {"E0": "KXEPL2H", "SP1": "KXLALIGA2H"}
SECOND_HALF_SPREAD_SERIES = {"E0": "KXEPL2HSPREAD", "SP1": "KXLALIGA2HSPREAD"}
SECOND_HALF_TOTAL_SERIES = {"E0": "KXEPL2HTOTAL", "SP1": "KXLALIGA2HTOTAL"}
SECOND_HALF_BTTS_SERIES = {"E0": "KXEPL2HBTTS", "SP1": "KXLALIGA2HBTTS"}

FTTS_SERIES = {
    "E0": "KXEPLFTTS", "SP1": "KXLALIGAFTTS", "I1": "KXSERIEAFTTS",
    "D1": "KXBUNDESLIGAFTTS", "F1": "KXLIGUE1FTTS", "MLS": "KXMLSFTTS",
}
SCORE_SERIES = {
    "E0": "KXEPLSCORE", "SP1": "KXLALIGASCORE", "I1": "KXSERIEASCORE",
    "D1": "KXBUNDESLIGASCORE", "F1": "KXLIGUE1SCORE", "MLS": "KXMLSSCORE",
}
TEAMTOTAL_SERIES = {
    "E0": "KXEPLTEAMTOTAL", "SP1": "KXLALIGATEAMTOTAL", "I1": "KXSERIEATEAMTOTAL",
    "D1": "KXBUNDESLIGATEAMTOTAL", "F1": "KXLIGUE1TEAMTOTAL", "MLS": "KXMLSTEAMTOTAL",
    "E1": "KXEFLCHAMPIONSHIPTEAMTOTAL",
}


def _get_half_winner_markets(series: dict, half: int) -> list[dict]:
    """Shared by First/Second Half Winner -- same real 3-way shape as
    get_moneyline_markets, but the tie-side label is "Tie 1st Half"/
    "Tie 2nd Half" (confirmed live for 1st Half via a real KXMLS1H event),
    not the bare "Tie" moneyline uses, and the team-side label is
    "{team} wins 1st/2nd Half", not the bare team name -- genuinely
    different label conventions, not reusable via get_moneyline_markets
    with a parameter swap alone."""
    half_word = "1st Half" if half == 1 else "2nd Half"
    rows = []
    for division, series_ticker in series.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                label = m.get("yes_sub_title", "")
                if label == f"Tie {half_word}":
                    side, team = "draw", None
                elif label == f"{home_team} wins {half_word}":
                    side, team = "home", home_team
                elif label == f"{away_team} wins {half_word}":
                    side, team = "away", away_team
                else:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team,
                    "ticker": m["ticker"], "side": side, "team": team,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_first_half_markets() -> list[dict]:
    return _get_half_winner_markets(FIRST_HALF_SERIES, 1)


def get_second_half_markets() -> list[dict]:
    return _get_half_winner_markets(SECOND_HALF_SERIES, 2)


_HALF_SPREAD_SUB_RE = re.compile(r"^(.+?) wins the (?:1H|2H) by more than [\d.]+ goals$")


def _get_half_spread_markets(series: dict) -> list[dict]:
    """Same shape as get_spread_markets, sub_title says "wins the 1H/2H by
    more than X goals" (confirmed live for 1H via a real KXMLS1HSPREAD
    event) instead of the full-match "wins by more than X goals"."""
    rows = []
    for division, series_ticker in series.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub_match = _HALF_SPREAD_SUB_RE.match(m.get("yes_sub_title", ""))
                if not sub_match:
                    continue
                name = sub_match.group(1)
                if name == home_team:
                    team = home_team
                elif name == away_team:
                    team = away_team
                else:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team,
                    "ticker": m["ticker"], "team": team, "line": _to_float(m.get("floor_strike")),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_first_half_spread_markets() -> list[dict]:
    return _get_half_spread_markets(FIRST_HALF_SPREAD_SERIES)


def get_second_half_spread_markets() -> list[dict]:
    return _get_half_spread_markets(SECOND_HALF_SPREAD_SERIES)


_HALF_TOTAL_SUB_RE = re.compile(r"^Over ([\d.]+) (?:1H|2H) goals scored$")


def _get_half_total_markets(series: dict) -> list[dict]:
    """Same shape as get_total_markets, sub_title says "Over X.5 1H/2H
    goals scored" (confirmed live for 1H via a real KXMLS1HTOTAL event)."""
    rows = []
    for division, series_ticker in series.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub_match = _HALF_TOTAL_SUB_RE.match(m.get("yes_sub_title", ""))
                if not sub_match:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team,
                    "ticker": m["ticker"], "line": float(sub_match.group(1)),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_first_half_total_markets() -> list[dict]:
    return _get_half_total_markets(FIRST_HALF_TOTAL_SERIES)


def get_second_half_total_markets() -> list[dict]:
    return _get_half_total_markets(SECOND_HALF_TOTAL_SERIES)


def _get_half_btts_markets(series: dict) -> list[dict]:
    """Same single-binary-market-per-event shape as get_btts_markets --
    reused directly (title suffix already added to _TITLE_SUFFIX_RE above,
    same "strip the known suffix, then parse teams" pattern)."""
    rows = []
    for division, series_ticker in series.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team, "ticker": m["ticker"],
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_first_half_btts_markets() -> list[dict]:
    return _get_half_btts_markets(FIRST_HALF_BTTS_SERIES)


def get_second_half_btts_markets() -> list[dict]:
    return _get_half_btts_markets(SECOND_HALF_BTTS_SERIES)


def get_ftts_markets() -> list[dict]:
    """First Team To Score -- real 3-way shape confirmed live via KXMLSFTTS
    (home team / away team / "No Goal"), genuinely different tie-analogue
    label ("No Goal", not "Tie") from every other 3-way market here."""
    rows = []
    for division, series_ticker in FTTS_SERIES.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                label = m.get("yes_sub_title", "")
                if label.lower() == "no goal":
                    side, team = "none", None
                elif label == home_team:
                    side, team = "home", home_team
                elif label == away_team:
                    side, team = "away", away_team
                else:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team,
                    "ticker": m["ticker"], "side": side, "team": team,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


_SCORE_SUB_RE = re.compile(r"^(.+?) (\d+) - (\d+) (.+?)$")


def get_correct_score_markets() -> list[dict]:
    """Real ladder confirmed live via KXMLSSCORE (30 real rungs per match,
    e.g. "San Jose Earthquakes wins 2-1" / "Draw 1-1"). yes_sub_title format
    differs for a draw ("Draw H-H") vs a decisive score ("{winner} wins
    H-A") -- home_score/away_score are derived from the TICKER suffix
    instead (e.g. "-SJ1LAG2"), which encodes both sides' goal counts in a
    fixed, unambiguous "{HOME_CODE}{h}{AWAY_CODE}{a}" shape regardless of
    which side won, rather than parsing two different real sub_title
    sentence shapes."""
    rows = []
    for division, series_ticker in SCORE_SERIES.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                ticker = m.get("ticker", "")
                suffix_match = re.search(r"-[A-Z]+(\d+)[A-Z]+(\d+)$", ticker)
                if not suffix_match:
                    continue
                home_score, away_score = int(suffix_match.group(1)), int(suffix_match.group(2))
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team,
                    "ticker": ticker, "home_score": home_score, "away_score": away_score,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


_TEAMTOTAL_SUB_RE = re.compile(r"^(.+?) over ([\d.]+) goals$")


def get_team_total_markets() -> list[dict]:
    """Real ladder confirmed live via KXMLSTEAMTOTAL (one side's OWN goal
    total, e.g. "San Jose over 1.5 goals" -- 3 lines x 2 teams per match)."""
    rows = []
    for division, series_ticker in TEAMTOTAL_SERIES.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub_match = _TEAMTOTAL_SUB_RE.match(m.get("yes_sub_title", ""))
                if not sub_match:
                    continue
                name, line = sub_match.group(1), float(sub_match.group(2))
                if name == home_team:
                    team = home_team
                elif name == away_team:
                    team = away_team
                else:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team,
                    "ticker": m["ticker"], "team": team, "line": line,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows



def get_top_n_markets() -> list[dict]:
    """One row per (threshold, team-in-the-field) -- see TOP_N_SERIES/
    _TOP_N_EVENT_LABELS above for the real event-ticker-suffix -> threshold
    mapping this dispatches on. Same real per-team-market shape as
    get_league_winner_markets/get_relegation_markets (single event per
    threshold, one binary market per team, no title-parsing needed)."""
    rows = []
    for division, series_ticker in TOP_N_SERIES.items():
        for ev in get_open_events(series_ticker):
            event_ticker = ev.get("event_ticker", "")
            suffix = event_ticker.rsplit("-", 1)[-1].replace(str(division), "").lstrip("0123456789")
            label_info = None
            for key, info in _TOP_N_EVENT_LABELS.items():
                if event_ticker.endswith(key):
                    label_info = info
                    break
            if label_info is None:
                continue
            threshold, group_label = label_info
            try:
                markets = get_markets_for_event(event_ticker)
            except Exception:
                continue
            for m in markets:
                team = m.get("yes_sub_title", "")
                if not team:
                    continue
                rows.append({
                    "event_ticker": event_ticker,
                    "division": division,
                    "threshold": threshold,
                    "group_label": group_label,
                    "ticker": m["ticker"],
                    "team": team,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


# ---------------------------------------------------------------------------
# DOMESTIC CUPS (2026-08-08). Different from every league series above in one
# structural way: a cup tie can pair clubs from DIFFERENT divisions, so the
# division is a property of each CLUB, not of the series. Callers get the
# competition and both tiers, and resolve each club's own division themselves
# (app/models/cup_match.py does the rating conversion).
#
# SCOPE IS DELIBERATE. check_cup_market_coverage.py measured the live inventory:
# Coppa Italia is 81% priceable because it starts at Serie A/B, but the DFB
# Pokal is only 40% and structurally capped -- its first round pairs Bundesliga
# clubs with REGIONALLIGA sides (Grossaspach, Hemelingen, Viktoria Cologne,
# Luneburg, St. Tonis), third and fourth tier, two divisions below anything
# football-data publishes for Germany. Both are ingested anyway: unrateable ties
# simply price as None, exactly like an unrated league club, and the Pokal's
# coverage improves in later rounds as the minnows are eliminated. Ingesting
# them cannot produce a bad bet -- the rating gate decides that.
CUP_COMPETITIONS = {
    "coppa_italia": {
        "name": "Coppa Italia",
        "top": "I1", "second": "I2",
        "moneyline": "KXCOPPAITALIAGAME",
        "advance": "KXCOPPAITALIAADVANCE",
        "total": "KXCOPPAITALIATOTAL",
    },
    "dfb_pokal": {
        "name": "DFB Pokal",
        "top": "D1", "second": "D2",
        "moneyline": "KXDFBPOKALGAME",
        "advance": "KXDFBPOKALADVANCE",
        "spread": "KXDFBPOKALSPREAD",   # 120 open 2026-08-18
        # WAS None with the note "no live total series for the Pokal as of
        # 2026-08-08". That went STALE and nothing re-checked it: probed
        # 2026-08-18 and KXDFBPOKALTOTAL has 180 open markets, every one quoted.
        # Same shape as the futures "404" note that outlived its 404 -- a dated
        # observation kept working as a permanent verdict. The re-probe is now
        # part of the routine, not a one-off (see the note on UEFA spread).
        "total": "KXDFBPOKALTOTAL",
    },
    # EFL Cup (the Carabao Cup -- Kalshi files it under EFL, not the sponsor
    # name, which is why a KXCARABAO* probe returns nothing). Added 2026-08-08
    # after a user asked whether it was covered; it was not, and all four of its
    # series sat dispositioned not_relevant.
    #
    # Same coverage caveat as the DFB Pokal, for the same structural reason:
    # the EFL Cup admits all four English professional tiers, and this app rates
    # only E0 and E1. The live first round is Plymouth (League One) vs Exeter
    # (League Two), neither of which is rateable. Coverage improves sharply from
    # round three, when Premier League clubs enter. Ingested anyway -- an
    # unrateable tie simply prices as None, exactly like an unrated league club,
    # and the alternative is noticing in October that nothing was collected.
    "efl_cup": {
        "name": "EFL Cup",
        "top": "E0", "second": "E1",
        "moneyline": "KXEFLCUPGAME",
        "advance": "KXEFLCUPADVANCE",
        "total": "KXEFLCUPTOTAL",
    },
    # Trophee des Champions. A DOMESTIC cup, not a cross-country one: it is the
    # Ligue 1 champion against the Coupe de France winner, so both sides are
    # normally F1 and cup_match takes its same-tier branch -- no bridge applied
    # and no caution raised, which is correct. F2 is still named as the second
    # tier for the rare year a Ligue 2 side wins the Coupe de France.
    # Live 2026-08-16: Lens vs PSG, both F1. Single match, no advance/total.
    "fra_super_cup": {
        "name": "France Super Cup",
        "top": "F1", "second": "F2",
        "moneyline": "KXFRASUPERCUPGAME",
        "advance": None,
        "total": None,
        "spread": None,
    },
    # DFL-Supercup, 2026-08-18, surfaced by the catalog scan rather than by a
    # probe -- which is the New Markets page doing its job. Structurally the
    # twin of the Trophee des Champions above: Bundesliga champion vs DFB-Pokal
    # winner, so both sides are normally D1 and cup_match takes its same-tier
    # branch with no bridge and no caution. D2 is named for the rare year a
    # 2. Bundesliga side wins the Pokal.
    #
    # Live at wiring: ESPN ger.super_cup returns the single fixture -- Bayern
    # Munich at Borussia Dortmund, both D1 and both rated -- and Kalshi has
    # GAME 3 / SPREAD 4 / TOTAL 6 open, all quoted, volume 0. Untraded, so
    # expect a tracked price rather than a bet, same as VEN1 and G1.
    #
    # ADVANCE is None because there is nothing to advance to: it is a one-off
    # final, decided on the day.
    "ger_super_cup": {
        "name": "German Super Cup",
        "top": "D1", "second": "D2",
        "moneyline": "KXGERSCGAME",
        "advance": None,
        "total": "KXGERSCTOTAL",
        "spread": "KXGERSCSPREAD",
    },
}

# An ADVANCE event titles itself "Home vs Away: X To Advance", so the pair has
# to be taken from the segment BEFORE the colon -- running _parse_title_teams
# over the whole string would try to read the outcome clause as a team name.
_ADVANCE_SUB_RE = re.compile(r"^(.+?)\s+advances$", re.IGNORECASE)
_REG_TIME_PREFIX = re.compile(r"^Reg(?:ulation)?\s*Time:\s*", re.IGNORECASE)


def _cup_pair(title: str) -> tuple[str, str] | None:
    return _parse_title_teams(title.split(":", 1)[0].strip())


def _cup_row(cup: str, cfg: dict, ev: dict, m: dict, home: str, away: str, **extra) -> dict:
    row = {
        "event_ticker": ev["event_ticker"],
        "event_title": ev.get("title", ""),
        "competition": cup,
        "competition_name": cfg["name"],
        "top_division": cfg["top"],
        "second_division": cfg["second"],
        "home_team": home,
        "away_team": away,
        "ticker": m["ticker"],
        "estimated_start_time": _kickoff_from_occurrence(m.get("occurrence_datetime")),
        "yes_bid": _to_float(m.get("yes_bid_dollars")),
        "yes_ask": _to_float(m.get("yes_ask_dollars")),
        "last_price": _to_float(m.get("last_price_dollars")),
        "volume": _to_float(m.get("volume_fp")),
        "status": m.get("status"),
    }
    row.update(extra)
    return row


def get_cup_moneyline_markets() -> list[dict]:
    """3-way cup moneyline -- home/away/draw at 90 minutes, same shape as
    get_moneyline_markets. NOTE these settle on REGULATION only; who actually
    progresses is the separate ADVANCE series below."""
    rows = []
    for cup, cfg in CUP_COMPETITIONS.items():
        for ev in get_open_events(cfg["moneyline"]):
            teams = _cup_pair(ev.get("title", ""))
            if teams is None:
                continue
            home, away = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                # Cup moneyline labels carry a "Reg Time: " prefix that league
                # ones do not ("Reg Time: Tie", "Reg Time: L.R. Vicenza"). That
                # prefix is also the settlement rule stated out loud: these
                # resolve on 90 minutes, NOT on who eventually progressed.
                label = _REG_TIME_PREFIX.sub("", (m.get("yes_sub_title") or "").strip())
                if label.lower() == "tie":
                    side, team = "draw", None
                elif label == home:
                    side, team = "home", home
                elif label == away:
                    side, team = "away", away
                else:
                    continue  # never guess which club an unrecognised label means
                rows.append(_cup_row(cup, cfg, ev, m, home, away, side=side, team=team))
    return rows


def get_cup_advance_markets() -> list[dict]:
    """Who progresses -- INCLUDING extra time and penalties, which is why this
    cannot be priced off the moneyline (see cup_match._advance_probs)."""
    rows = []
    for cup, cfg in CUP_COMPETITIONS.items():
        if not cfg.get("advance"):
            continue  # single-match cups (super cups) have no advance series
        for ev in get_open_events(cfg["advance"]):
            teams = _cup_pair(ev.get("title", ""))
            if teams is None:
                continue
            home, away = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub = _ADVANCE_SUB_RE.match((m.get("yes_sub_title") or "").strip())
                if not sub:
                    continue
                who = sub.group(1).strip()
                if who == home:
                    side, team = "home", home
                elif who == away:
                    side, team = "away", away
                else:
                    continue
                rows.append(_cup_row(cup, cfg, ev, m, home, away, side=side, team=team))
    return rows


def get_cup_total_markets() -> list[dict]:
    """Over/under total goals in REGULATION. The pair is not in the market
    title here ("Will over 4.5 goals be scored?"), so it comes from the EVENT
    title, and the line comes from yes_sub_title."""
    rows = []
    for cup, cfg in CUP_COMPETITIONS.items():
        series = cfg.get("total")
        if not series:
            continue
        for ev in get_open_events(series):
            teams = _cup_pair(ev.get("title", ""))
            if teams is None:
                continue
            home, away = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                mt = re.search(r"([\d.]+)", m.get("yes_sub_title") or m.get("title") or "")
                if not mt:
                    continue
                try:
                    line = float(mt.group(1))
                except ValueError:
                    continue
                rows.append(_cup_row(cup, cfg, ev, m, home, away, line=line, side="over"))
    return rows


# --- UEFA CLUB COMPETITIONS (2026-08-08) -----------------------------------
# Cross-COUNTRY, so unlike the domestic cups above there is no "top/second tier"
# pair -- each club's league is resolved individually and converted with the
# fitted strength offsets (app/models/uefa_match.py).
#
# ADVANCE IS DELIBERATELY NOT INGESTED. KXUCLADVANCE and friends exist and have
# live inventory, but UEFA knockout ties are decided over TWO LEGS plus extra
# time, so "to advance" depends on an aggregate score across two matches. The
# single-leg formula that prices domestic cup advancement would be wrong here
# (see uefa_match.py's own docstring), and pricing it off the single-match
# distribution would be worse than not pricing it. GAME and TOTAL settle on one
# match's regulation result and are fine.
#
# SPREAD IS NOW INGESTED, and the note that deferred it was wrong on its own
# premise. It claimed the yes_sub_title used "a different shape from the league
# spread parser ('Goal Diff Reg Time: <team> ...')". Re-probed 2026-08-18:
# KXUCLSPREAD reads "Celtic wins by more than 2.5 goals" -- BYTE-IDENTICAL in
# shape to KXEPLSPREAD. No bespoke parser was ever needed; the same
# "<team> wins by more than X goals" regex the leagues use handles it, with the
# existing _REG_TIME_PREFIX strip covering the Pokal, which does prefix it.
# The "only 16 live rows" half was true when written and is not now: 29 UCL,
# 48 UEL, 96 UECL, 120 DFB Pokal open markets at the same probe.
UEFA_COMPETITIONS = {
    "ucl": {"name": "Champions League", "moneyline": "KXUCLGAME", "total": "KXUCLTOTAL",
            "spread": "KXUCLSPREAD"},
    "uel": {"name": "Europa League", "moneyline": "KXUELGAME", "total": "KXUELTOTAL",
            "spread": "KXUELSPREAD"},
    "uecl": {"name": "Conference League", "moneyline": "KXUECLGAME", "total": "KXUECLTOTAL",
             "spread": "KXUECLSPREAD"},
    # UEFA Super Cup -- one match a year, UCL winner vs UEL winner, so it is
    # cross-COUNTRY by construction and the fitted league offsets are exactly
    # the right tool. Live 2026-08-12: PSG (F1) vs Aston Villa (E0), both rated.
    # No total series is listed for it.
    "usc": {"name": "UEFA Super Cup", "moneyline": "KXUEFASCGAME", "total": None,
            "spread": None},   # KXUEFASCSPREAD exists but had 0 open at the 2026-08-18 probe
}


# ---- Leagues Cup (MLS vs Liga MX), added 2026-08-08 ------------------------
# Kept SEPARATE from UEFA_COMPETITIONS on purpose. The shape is the same
# (cross-league, one match, moneyline + total + spread + BTTS) but the MODEL is
# not: models/leagues_cup_match.py carries its own fitted offsets AND its own
# venue term (+0.0071, essentially neutral -- the early editions were played
# entirely in the US/Canada). Routing these rows through the UEFA handler would
# price them with European offsets and a full domestic home advantage, both
# wrong. See leagues_cup_match.py's docstring.
#
# Confirmed live 2026-08-08 -- and the label shape differs from the domestic
# cups in a way that matters: there is NO "Reg Time: " prefix here, labels are
# plain "Tie" / "Miami" / "Monterrey" and match the event title's own short
# names. The prefix strip is still applied so that a future format change
# cannot silently drop every row.
LEAGUES_CUP = {
    "name": "Leagues Cup",
    # ADVANCE, added 2026-08-18 from the New Markets queue. Safe here where the
    # UEFA/CONMEBOL equivalents are not: the Leagues Cup knockout is a SINGLE
    # match that goes straight to penalties, so advancing is a property of the
    # one game in front of you. 8 open markets, all quoted, volume 3,097 -- the
    # only advance series in the queue with real trade behind it.
    "advance": "KXLEAGUESCUPADVANCE",
    "moneyline": "KXLEAGUESCUPGAME",
    "total": "KXLEAGUESCUPTOTAL",
    "spread": "KXLEAGUESCUPSPREAD",
    "btts": "KXLEAGUESCUPBTTS",
}


def _leagues_cup_row(ev: dict, m: dict, home: str, away: str, **extra) -> dict:
    row = {
        "event_ticker": ev["event_ticker"], "event_title": ev.get("title", ""),
        "competition": "leagues_cup", "competition_name": LEAGUES_CUP["name"],
        "home_team": home, "away_team": away,
        "ticker": m["ticker"], "estimated_start_time": _kickoff_from_occurrence(m.get("occurrence_datetime")),
        "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
        "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
        "status": m.get("status"),
    }
    row.update(extra)
    return row


def _leagues_cup_events(series: str):
    """(event, home, away) for each parseable open event of a Leagues Cup
    series. Skips anything whose title does not give a clean pair rather than
    guessing at the sides."""
    for ev in get_open_events(series):
        teams = _cup_pair(ev.get("title", ""))
        if teams is None:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        yield ev, teams[0], teams[1], markets


def get_leagues_cup_moneyline_markets() -> list[dict]:
    """3-way regulation moneyline."""
    rows = []
    for ev, home, away, markets in _leagues_cup_events(LEAGUES_CUP["moneyline"]):
        for m in markets:
            label = _REG_TIME_PREFIX.sub("", (m.get("yes_sub_title") or "").strip())
            if label.lower() == "tie":
                side, team = "draw", None
            elif label == home:
                side, team = "home", home
            elif label == away:
                side, team = "away", away
            else:
                continue  # never guess which club an unrecognised label means
            rows.append(_leagues_cup_row(ev, m, home, away, side=side, team=team))
    return rows


def get_leagues_cup_total_markets() -> list[dict]:
    """Over/under total goals in regulation."""
    rows = []
    for ev, home, away, markets in _leagues_cup_events(LEAGUES_CUP["total"]):
        for m in markets:
            mt = re.search(r"([\d.]+)", m.get("yes_sub_title") or m.get("title") or "")
            if not mt:
                continue
            try:
                line = float(mt.group(1))
            except ValueError:
                continue
            rows.append(_leagues_cup_row(ev, m, home, away, line=line, side="over"))
    return rows


def get_leagues_cup_advance_markets() -> list[dict]:
    """"<Home> vs <Away>: <Team> To Advance". The pair comes from the segment
    BEFORE the colon, same as the domestic cup advance reader."""
    rows = []
    for ev, home, away, markets in _leagues_cup_events(LEAGUES_CUP["advance"]):
        for m in markets:
            sub = _ADVANCE_SUB_RE.match((m.get("yes_sub_title") or "").strip())
            if not sub:
                continue
            who = sub.group(1).strip()
            if who == home:
                side, team = "home", home
            elif who == away:
                side, team = "away", away
            else:
                continue
            rows.append(_leagues_cup_row(ev, m, home, away, side=side, team=team))
    return rows


def get_leagues_cup_spread_markets() -> list[dict]:
    """Goal handicap. Sub-title is "<Team> wins by more than X.5 goals", so the
    team is read from the label and the line from the number -- the same
    approach as the league spread reader."""
    rows = []
    for ev, home, away, markets in _leagues_cup_events(LEAGUES_CUP["spread"]):
        for m in markets:
            sub = (m.get("yes_sub_title") or "").strip()
            mt = re.match(r"^(.*?)\s+wins by more than\s+([\d.]+)\s+goals?", sub)
            if not mt:
                continue
            label, raw_line = mt.group(1).strip(), mt.group(2)
            if label == home:
                side, team = "home", home
            elif label == away:
                side, team = "away", away
            else:
                continue
            try:
                line = float(raw_line)
            except ValueError:
                continue
            rows.append(_leagues_cup_row(ev, m, home, away, line=line, side=side, team=team))
    return rows


def get_leagues_cup_btts_markets() -> list[dict]:
    """Both teams to score."""
    rows = []
    for ev, home, away, markets in _leagues_cup_events(LEAGUES_CUP["btts"]):
        for m in markets:
            if "both teams to score" not in (m.get("yes_sub_title") or "").lower():
                continue
            rows.append(_leagues_cup_row(ev, m, home, away, side="yes"))
    return rows


# ---- National teams: ASEAN Championship, added 2026-08-09 -------------------
# Ratings come from the INTL pool (ingestion/international_data.py) and pricing
# from models/national_match.py, which refuses any cross-confederation fixture.
# Every ASEAN fixture is AFC-vs-AFC, so that gate never bites here -- it exists
# for the day Kalshi lists a World Cup or an inter-confederation friendly.
#
# ADVANCE is deliberately not fetched. KXASEANADVANCE is a knockout progression
# question, decided after extra time and penalties, which a single-match goal
# distribution cannot answer -- the same reason cup_advance is tracking-only and
# UEFA advance is not ingested at all. Ingesting it would only mint rows this
# app must then refuse to price.
NATIONAL_COMPETITIONS = {
    "asean": {
        "name": "ASEAN Championship",
        "moneyline": "KXASEANGAME",
        "total": "KXASEANTOTAL",
        "spread": "KXASEANSPREAD",
        "btts": "KXASEANBTTS",
    },
}


def _national_row(comp: str, cfg: dict, ev: dict, m: dict, home: str, away: str, **extra) -> dict:
    row = {
        "event_ticker": ev["event_ticker"], "event_title": ev.get("title", ""),
        "competition": comp, "competition_name": cfg["name"],
        "home_team": home, "away_team": away,
        "ticker": m["ticker"], "estimated_start_time": _kickoff_from_occurrence(m.get("occurrence_datetime")),
        "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
        "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
        "status": m.get("status"),
    }
    row.update(extra)
    return row


def _national_events(series: str):
    for ev in get_open_events(series):
        teams = _cup_pair(ev.get("title", ""))
        if teams is None:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        yield ev, teams[0], teams[1], markets


def get_national_moneyline_markets() -> list[dict]:
    """3-way regulation moneyline. Labels carry the "Reg Time: " prefix, which
    is also the settlement rule stated out loud -- these resolve on 90 minutes,
    not on who eventually progressed."""
    rows = []
    for comp, cfg in NATIONAL_COMPETITIONS.items():
        for ev, home, away, markets in _national_events(cfg["moneyline"]):
            for m in markets:
                label = _REG_TIME_PREFIX.sub("", (m.get("yes_sub_title") or "").strip())
                if label.lower() == "tie":
                    side, team = "draw", None
                elif label == home:
                    side, team = "home", home
                elif label == away:
                    side, team = "away", away
                else:
                    continue
                rows.append(_national_row(comp, cfg, ev, m, home, away, side=side, team=team))
    return rows


def get_national_total_markets() -> list[dict]:
    rows = []
    for comp, cfg in NATIONAL_COMPETITIONS.items():
        for ev, home, away, markets in _national_events(cfg["total"]):
            for m in markets:
                mt = re.search(r"([\d.]+)", m.get("yes_sub_title") or m.get("title") or "")
                if not mt:
                    continue
                try:
                    line = float(mt.group(1))
                except ValueError:
                    continue
                rows.append(_national_row(comp, cfg, ev, m, home, away, line=line, side="over"))
    return rows


def get_national_spread_markets() -> list[dict]:
    """Goal handicap. The sub-title carries a "Goal Diff Reg Time: " prefix that
    the club spread markets do not, so the team/line are parsed after stripping
    everything up to the last colon."""
    rows = []
    for comp, cfg in NATIONAL_COMPETITIONS.items():
        for ev, home, away, markets in _national_events(cfg["spread"]):
            for m in markets:
                sub = (m.get("yes_sub_title") or "").strip()
                sub = sub.rsplit(":", 1)[-1].strip() if ":" in sub else sub
                mt = re.match(r"^(.*?)\s+wins by more than\s+([\d.]+)\s+goals?", sub)
                if not mt:
                    continue
                label, raw_line = mt.group(1).strip(), mt.group(2)
                if label == home:
                    side, team = "home", home
                elif label == away:
                    side, team = "away", away
                else:
                    continue
                try:
                    line = float(raw_line)
                except ValueError:
                    continue
                rows.append(_national_row(comp, cfg, ev, m, home, away, line=line, side=side, team=team))
    return rows


def get_national_btts_markets() -> list[dict]:
    rows = []
    for comp, cfg in NATIONAL_COMPETITIONS.items():
        for ev, home, away, markets in _national_events(cfg["btts"]):
            for m in markets:
                if "both teams to score" not in (m.get("yes_sub_title") or "").lower():
                    continue
                rows.append(_national_row(comp, cfg, ev, m, home, away, side="yes"))
    return rows


def get_uefa_spread_markets() -> list[dict]:
    """Goal handicap on the single match, regulation time.

    SETTLES ON ONE LEG, which is why this is safe where "to advance" is not: a
    two-legged tie's ADVANCE market depends on an aggregate score across two
    matches, but its SPREAD is a property of the match in front of you. Same
    reasoning that already lets uefa_moneyline_3way and uefa_total through."""
    rows = []
    for comp, cfg in UEFA_COMPETITIONS.items():
        series = cfg.get("spread")
        if not series:
            continue
        for ev in get_open_events(series):
            teams = _cup_pair(ev.get("title", ""))
            if teams is None:
                continue
            home, away = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                parsed = _parse_spread_sub(m.get("yes_sub_title"), home, away)
                if parsed is None:
                    continue
                side, team, line = parsed
                rows.append(_uefa_row(comp, cfg, ev, m, home, away,
                                      line=line, side=side, team=team))
    return rows


def get_cup_spread_markets() -> list[dict]:
    """Domestic-cup goal handicap, regulation time. Same single-leg argument as
    the UEFA one above."""
    rows = []
    for cup, cfg in CUP_COMPETITIONS.items():
        series = cfg.get("spread")
        if not series:
            continue
        for ev in get_open_events(series):
            teams = _cup_pair(ev.get("title", ""))
            if teams is None:
                continue
            home, away = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                parsed = _parse_spread_sub(m.get("yes_sub_title"), home, away)
                if parsed is None:
                    continue
                side, team, line = parsed
                rows.append(_cup_row(cup, cfg, ev, m, home, away,
                                     line=line, side=side, team=team))
    return rows


# ---- CONMEBOL (2026-08-18) -------------------------------------------------
# Copa Libertadores and Copa Sudamericana. Kept SEPARATE from UEFA_COMPETITIONS
# for the same reason the Leagues Cup is: the shape is identical (cross-country,
# one match, moneyline + total + spread) but models/conmebol_match.py carries its
# OWN fitted offsets and its own baseline mu, pinned on BRA1. Routing these rows
# through the UEFA handler would price a Brazilian club with European offsets.
#
# ADVANCE IS DELIBERATELY NOT INGESTED, same rule as UEFA: CONMEBOL knockout
# rounds are two legs plus penalties, so KXCONMEBOLLIBADVANCE depends on an
# aggregate across two matches. There are 14 open advance markets and they stay
# uningested until the two-legged model exists.
#
# BTTS and the 1H family are also skipped -- the app takes those for leagues but
# has no cross-league BTTS path, and inventing one for the smallest slice here
# would be building ahead of the evidence.
CONMEBOL_COMPETITIONS = {
    "libertadores": {
        "name": "Copa Libertadores",
        "moneyline": "KXCONMEBOLLIBGAME",
        "total": "KXCONMEBOLLIBTOTAL",
        "spread": "KXCONMEBOLLIBSPREAD",
    },
    "sudamericana": {
        "name": "Copa Sudamericana",
        "moneyline": "KXCONMEBOLSUDGAME",
        "total": "KXCONMEBOLSUDTOTAL",
        "spread": "KXCONMEBOLSUDSPREAD",
    },
}


def _conmebol_row(comp: str, cfg: dict, ev: dict, m: dict, home: str, away: str, **extra) -> dict:
    row = {
        "event_ticker": ev["event_ticker"], "event_title": ev.get("title", ""),
        "competition": comp, "competition_name": cfg["name"],
        "home_team": home, "away_team": away,
        "ticker": m["ticker"], "estimated_start_time": _kickoff_from_occurrence(m.get("occurrence_datetime")),
        "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
        "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
        "status": m.get("status"),
    }
    row.update(extra)
    return row


def _conmebol_events(series: str):
    for ev in get_open_events(series):
        teams = _cup_pair(ev.get("title", ""))
        if teams is None:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        yield ev, teams[0], teams[1], markets


def get_conmebol_moneyline_markets() -> list[dict]:
    """Regulation-time 3-way. Labels carry the "Reg Time: " prefix."""
    rows = []
    for comp, cfg in CONMEBOL_COMPETITIONS.items():
        for ev, home, away, markets in _conmebol_events(cfg["moneyline"]):
            for m in markets:
                label = _REG_TIME_PREFIX.sub("", (m.get("yes_sub_title") or "").strip())
                if label.lower() == "tie":
                    side, team = "draw", None
                elif label == home:
                    side, team = "home", home
                elif label == away:
                    side, team = "away", away
                else:
                    continue   # never guess which club an unrecognised label means
                rows.append(_conmebol_row(comp, cfg, ev, m, home, away, side=side, team=team))
    return rows


def get_conmebol_total_markets() -> list[dict]:
    rows = []
    for comp, cfg in CONMEBOL_COMPETITIONS.items():
        for ev, home, away, markets in _conmebol_events(cfg["total"]):
            for m in markets:
                mt = re.search(r"([\d.]+)", m.get("yes_sub_title") or m.get("title") or "")
                if not mt:
                    continue
                try:
                    line = float(mt.group(1))
                except ValueError:
                    continue
                rows.append(_conmebol_row(comp, cfg, ev, m, home, away, line=line, side="over"))
    return rows


def get_conmebol_spread_markets() -> list[dict]:
    """Goal handicap on the single leg -- see get_uefa_spread_markets for why a
    spread is safe on a two-legged tie where an ADVANCE market is not."""
    rows = []
    for comp, cfg in CONMEBOL_COMPETITIONS.items():
        for ev, home, away, markets in _conmebol_events(cfg["spread"]):
            for m in markets:
                parsed = _parse_spread_sub(m.get("yes_sub_title"), home, away)
                if parsed is None:
                    continue
                side, team, line = parsed
                rows.append(_conmebol_row(comp, cfg, ev, m, home, away,
                                          line=line, side=side, team=team))
    return rows


def _uefa_row(comp: str, cfg: dict, ev: dict, m: dict, home: str, away: str, **extra) -> dict:
    row = {
        "event_ticker": ev["event_ticker"], "event_title": ev.get("title", ""),
        "competition": comp, "competition_name": cfg["name"],
        "home_team": home, "away_team": away,
        "ticker": m["ticker"], "estimated_start_time": _kickoff_from_occurrence(m.get("occurrence_datetime")),
        "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
        "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
        "status": m.get("status"),
    }
    row.update(extra)
    return row


def get_uefa_moneyline_markets() -> list[dict]:
    """Regulation-time 3-way for a single UEFA match. Labels carry the same
    "Reg Time: " prefix the domestic cups use."""
    rows = []
    for comp, cfg in UEFA_COMPETITIONS.items():
        for ev in get_open_events(cfg["moneyline"]):
            teams = _cup_pair(ev.get("title", ""))
            if teams is None:
                continue
            home, away = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                label = _REG_TIME_PREFIX.sub("", (m.get("yes_sub_title") or "").strip())
                if label.lower() == "tie":
                    side, team = "draw", None
                elif label == home:
                    side, team = "home", home
                elif label == away:
                    side, team = "away", away
                else:
                    continue
                rows.append(_uefa_row(comp, cfg, ev, m, home, away, side=side, team=team))
    return rows


def get_uefa_total_markets() -> list[dict]:
    """Over/under total goals in regulation."""
    rows = []
    for comp, cfg in UEFA_COMPETITIONS.items():
        for ev in get_open_events(cfg["total"]):
            teams = _cup_pair(ev.get("title", ""))
            if teams is None:
                continue
            home, away = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                mt = re.search(r"([\d.]+)", m.get("yes_sub_title") or m.get("title") or "")
                if not mt:
                    continue
                try:
                    line = float(mt.group(1))
                except ValueError:
                    continue
                rows.append(_uefa_row(comp, cfg, ev, m, home, away, line=line, side="over"))
    return rows


# The one parser both cross-league spread readers share. Kept as a module-level
# helper rather than duplicated so a Kalshi wording change is a one-line fix in
# one place -- the shape is identical for UEFA, the domestic cups and the
# leagues, and the only difference is the Pokal's "Reg Time: " prefix.
_CROSS_SPREAD_SUB_RE = re.compile(r"^(.*?)\s+wins by more than\s+([\d.]+)\s+goals?", re.IGNORECASE)


def _parse_spread_sub(sub: str, home: str, away: str):
    """(side, team, line) for a "<team> wins by more than X goals" label, or
    None when the label names neither side -- never a guess."""
    mt = _CROSS_SPREAD_SUB_RE.match(_REG_TIME_PREFIX.sub("", (sub or "").strip()))
    if not mt:
        return None
    label = mt.group(1).strip()
    if label == home:
        side, team = "home", home
    elif label == away:
        side, team = "away", away
    else:
        return None
    try:
        return side, team, float(mt.group(2))
    except ValueError:
        return None
