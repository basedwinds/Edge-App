"""Matches Kalshi/Polymarket Soccer markets to this app's SoccerMatch rows.
Parallel to market_matcher_tennis.py/market_matcher_mma.py, but a THIRD
different name-matching problem: unlike Tennis (abbreviated-vs-full-name)
or MMA (both sides already render full fighter names), soccer club names
have genuine SHORTHAND/NICKNAME variants that aren't decomposable by any
truncation rule -- football-data.co.uk's own historical CSVs use short
in-house names ("Man United", "Wolves", "Spurs"), while Kalshi/Polymarket's
live listings render full or semi-full club names ("Manchester United",
"Wolverhampton Wanderers", "Tottenham") -- confirmed live 2026-07-19 from
real open Kalshi markets ("Liverpool vs Brentford", "San Jose vs Los Angeles
G" for MLS, where "Los Angeles G" is Kalshi's own disambiguation of LA
Galaxy from LAFC).

A plain token-subset match (MMA's/Tennis's full_names_match approach) fails
on these pairs outright ({"man","united"} is not a subset of {"manchester",
"united"} or vice versa) -- so this needs a hardcoded ALIAS TABLE normalized
BEFORE the token-subset comparison. This alias table is NOT exhaustive (only
the well-documented, stable EPL/football-data.co.uk shorthand set below is
covered at ship time) -- same "known, accepted gap" category as Tennis's
surname+initial collision risk or NFL's backup-QB matching: a genuinely new
club-name variant this table hasn't seen will simply fail to match and show
up as a real, visible miss (no SoccerMatch found), not a silent
mismatch -- extend TEAM_ALIASES as real gaps are found live, don't try to
guess every league's shorthand upfront."""
from __future__ import annotations

import json as _json
import logging as _logging
from pathlib import Path as _Path

from app.ingestion.soccer_data import normalize_team_name

_log = _logging.getLogger(__name__)

# football-data.co.uk's own shorthand -> a canonical full-ish name, chosen to
# match how Kalshi/Polymarket tend to render the same club (not necessarily
# the club's full legal name). Keys are ALREADY normalize_team_name()'d.
# EPL-only at ship time (the league this app's audit spent the most time
# confirming both sides' real naming) -- La Liga/Serie A/Bundesliga/Ligue 1/
# MLS entries added as real mismatches are found live, not guessed upfront.
TEAM_ALIASES: dict[str, str] = {
    "man united": "manchester united",
    "man utd": "manchester united",
    "man city": "manchester city",
    "spurs": "tottenham",
    "tottenham hotspur": "tottenham",
    "wolves": "wolverhampton wanderers",
    "nottm forest": "nottingham forest",
    "nott'm forest": "nottingham forest",
    "leicester": "leicester city",
    "newcastle": "newcastle united",
    "west brom": "west bromwich albion",
    "west ham": "west ham united",
    "brighton": "brighton hove albion",
    "sheffield united": "sheffield united",
    "wolverhampton": "wolverhampton wanderers",
    # MLS: confirmed live 2026-07-19 -- this app's own first live poller run
    # against real Kalshi/Polymarket data found these two real mismatches
    # (ESPN's own names, the training-data source, on the right):
    # Kalshi's "Los Angeles G" (its own disambiguation of Galaxy vs LAFC) and
    # Polymarket's "Los Angeles FC" both fail a plain token-subset match
    # against ESPN's "LA Galaxy"/"LAFC" outright (no shared tokens at all,
    # unlike e.g. "San Jose" vs ESPN's "San Jose Earthquakes", which already
    # matches via token-subset with NO alias needed). NOT exhaustive for
    # every MLS/other-league naming gap -- same "extend as found live" policy
    # as the EPL set above.
    # MLS: confirmed live 2026-07-19 via a systematic scan (compared every
    # real team name Kalshi/Polymarket had actually produced in this app's
    # own DB, across the first live poller run, against every team name
    # ESPN's own training cache uses) -- Kalshi in particular renders MLS
    # teams as a bare CITY name in its market titles ("Houston" not "Houston
    # Dynamo FC"), which canonical_team_key() (an EXACT alias lookup, unlike
    # the fuzzy token-subset team_names_match() uses for cross-platform
    # listing matches) cannot resolve without an explicit entry per club.
    # Every value below is ESPN's own name for that club, normalized the
    # same way canonical_team_key() normalizes everything else.
    "atlanta": "atlanta united fc",
    "austin": "austin fc",
    "charlotte": "charlotte fc",
    # "chicago"/"dc"/"san diego" (city-only, no club word) are the exact labels
    # Kalshi uses on the MLS Cup / conference futures, found unmatched in a
    # 2026-08-07 sweep of all 30 against the ESPN conference table. Each is
    # unambiguous league-wide -- MLS has exactly one club in each of those
    # cities -- unlike the Los Angeles pair below, which is why that one needs
    # Kalshi's trailing F/G disambiguator and these do not.
    "chicago": "chicago fire fc",
    "chicago fire": "chicago fire fc",
    "cincinnati": "fc cincinnati",
    "colorado": "colorado rapids",
    "colorado rapids sc": "colorado rapids",
    "columbus": "columbus crew",
    "dc": "dc united",
    "dc united sc": "dc united",
    "dallas": "fc dallas",
    "houston": "houston dynamo fc",
    "houston dynamo": "houston dynamo fc",
    "kansas city": "sporting kansas city",
    "los angeles f": "lafc",
    "los angeles fc": "lafc",
    "los angeles g": "la galaxy",
    "los angeles galaxy": "la galaxy",
    "miami": "inter miami cf",
    "minnesota": "minnesota united fc",
    "montreal": "cf montreal",
    "nashville": "nashville sc",
    "new england": "new england revolution",
    "new york city": "new york city fc",
    "new york rb": "red bull new york",
    "new york red bulls": "red bull new york",
    "orlando": "orlando city sc",
    "philadelphia": "philadelphia union",
    "portland": "portland timbers",
    "saint louis": "st louis city sc",
    "salt lake": "real salt lake",
    # San Diego FC joined MLS in 2025, after this block was first written.
    "san diego": "san diego fc",
    "san jose": "san jose earthquakes",
    "seattle": "seattle sounders fc",
    # The FULLER renderings, found unrated in a 2026-08-07 sweep of every live
    # soccer market's team against the rating pools. The short keys above only
    # cover the city-only form some listings use; a listing that spells the club
    # out lands on neither the short key nor the ESPN cache's own spelling.
    "saint louis city sc": "st louis city sc",
    "seattle sounders": "seattle sounders fc",
    "toronto": "toronto fc",
    "vancouver": "vancouver whitecaps",
    "vancouver whitecaps fc": "vancouver whitecaps",
    # Big-5 league-winner FUTURES: confirmed live 2026-07-19 via a systematic
    # scan of every real team Kalshi's own KXPREMIERLEAGUE/KXLALIGA/KXSERIEA/
    # KXBUNDESLIGA/KXLIGUE1-27 winner markets list, compared against every
    # team football-data.co.uk's own historical top-flight cache uses. This
    # is the SAME real bug class as the MLS block above (a live market's own
    # naming doesn't byte-match the training-data source's naming) -- caught
    # here because a Poisson season Monte Carlo (season_sim_soccer.py)
    # showed the real EPL/Ligue 1 market FAVORITE (Arsenal/Man City's real
    # rival, PSG at a real 44% market price) landing at an impossible EXACT
    # 0.0 simulated-champion probability out of 3,000 seasons -- a team that
    # good landing at literally zero is a stronger tell than a merely-low
    # number, which is what made this worth chasing down as a real bug
    # rather than "the model just disagrees with the market."
    "hull city": "hull",
    "coventry city": "coventry",
    "leeds united": "leeds",
    "ipswich town": "ipswich",
    "athletic bilbao": "ath bilbao",
    "rayo vallecano": "vallecano",
    "atletico madrid": "ath madrid",
    "deportivo de la coruna": "la coruna",
    "celta vigo": "celta",
    "real sociedad": "sociedad",
    "racing santander": "santander",
    "espanyol": "espanol",
    "parma calcio": "parma",
    "frankfurt": "ein frankfurt",
    "schalke": "schalke 04",
    # Kalshi's own mangled-encoding rendering of "Mönchengladbach" (confirmed
    # live: the raw API response literally contains U+00B4 ACUTE ACCENT in
    # place of "ö", not a transcription choice on this app's side). The
    # alias KEY here is the ALREADY-NORMALIZED form ("m gladbach", with a
    # real space -- confirmed via normalize_team_name() directly, not typed
    # from how the raw string displays, since canonical_team_key() looks up
    # aliases AFTER normalizing, not before) -- football-data.co.uk's own
    # two apostrophe variants ("M'Gladbach"/"M'gladbach") already
    # canonicalize to "mgladbach" (no space) with NO alias needed, so this
    # only needs to redirect Kalshi's own broken form to that same target.
    "m gladbach": "mgladbach",
    "fc cologne": "fc koln",
    "bremen": "werder bremen",
    "strasbourg alsace": "strasbourg",
    "psg": "paris sg",
    "stade brest 29": "brest",
    "stade rennes": "rennes",
    # Kalshi lists "PSG" and "Paris" as two SEPARATE real markets (confirmed
    # live) -- football-data.co.uk's own cache also has two separate real
    # clubs, "Paris SG" and "Paris FC". Since "PSG" unambiguously means Paris
    # Saint-Germain (aliased above), Kalshi's bare "Paris" is the OTHER real
    # club by elimination, not a duplicate or a guess.
    "paris": "paris fc",
    # ---- La Liga (SP1), 2026-08-06 -------------------------------------------
    # DERIVED FROM REAL LISTINGS, not typed from football knowledge -- see
    # scripts/derive_soccer_team_aliases.py, which is kept so this can be re-run
    # for any league as new clubs appear.
    #
    # The gap these close: Polymarket lists La Liga under full official names
    # ("RC Celta de Vigo"), Kalshi under football-data.co.uk-style short names
    # ("Celta Vigo"), and the RATINGS are keyed on football-data's shortest form
    # ("celta"). With none of it bridged, 8 of 12 SP1 fixtures carrying active
    # markets had BOTH teams reading as unrated -- the league was effectively
    # unpriced -- and three fixtures had been ingested TWICE, once per platform's
    # spelling, because the same failure defeats match_upcoming_soccer_match.
    #
    # The evidence: both platforms list the SAME fixtures, so pairing them on
    # (division, date) via the side that already matches yields the other side's
    # two names as an OBSERVED pair. "Real Racing Club" is learned to be Kalshi's
    # "Santander" because its opponent Villarreal CF/Villarreal pins the fixture.
    #
    # Why that mattered more than it sounds: a plain token rule proposed
    # "rcd espanyol de barcelona" -> "barcelona" -- one candidate, unique, and
    # the WRONG CLUB, because Espanyol's official name contains its city and that
    # city is a rival club. Fixture alignment overruled it. Uniqueness alone is
    # not safety when the name contains another club's name.
    "atletico": "ath madrid",                    # Kalshi's bare "Atletico" -- ambiguous
                                                 # on its own (Madrid or Bilbao?), pinned by
                                                 # its Polymarket twin below
    "club atletico de madrid": "ath madrid",
    "ca osasuna": "osasuna",
    "deportivo alaves": "alaves",
    "elche cf": "elche",
    "getafe cf": "getafe",
    "levante ud": "levante",
    "malaga cf": "malaga",
    "rayo vallecano de madrid": "vallecano",
    "rc celta de vigo": "celta",
    "rc deportivo a coruna": "la coruna",
    "rcd espanyol de barcelona": "espanol",
    "real racing club": "santander",
    "sevilla fc": "sevilla",
    "villarreal cf": "villarreal",
    # The one entry NOT from cross-platform alignment: Real Betis is currently
    # listed by Kalshi only, so it has no twin to learn from. Accepted on four
    # independent checks instead: team_names_match("Real Betis", "Betis") ALREADY
    # returns True in shipped code (so this only makes canonical_team_key agree
    # with a judgement the module was already making), "betis" is the sole rated
    # SP1 key that is a strict token-subset of {real, betis}, it is in the
    # 2025-26 club set with 1,061 rated matches, and no other rated key contains
    # the token "betis".
    "real betis": "betis",
    # ---- Liga Portugal (P1), 2026-08-07 --------------------------------------
    # Polymarket does not list Liga Portugal at all, so the cross-platform
    # fixture alignment that derived the La Liga table above is UNAVAILABLE here.
    # These five are instead anchored on a shared distinctive token AND
    # constrained to the clubs actually in football-data's current P1 season, so
    # each has exactly one candidate:
    "sl benfica": "benfica",                 # "benfica"
    "santa clara azores": "santa clara",     # "santa clara"
    "vicente barcelos": "gil vicente",       # "vicente" (Gil Vicente play in Barcelos)
    "braga": "sp braga",                     # "braga"
    # "estrela" and "est amadora" BOTH exist as historical keys; restricting to
    # the current season's club list leaves only "estrela", which is what makes
    # this one safe rather than a coin flip between two spellings of the club.
    "estrela amadora": "estrela",
    #
    # "Sporting CP" -> "sp lisbon". This was DELIBERATELY LEFT UNMAPPED at first,
    # on the reasoning that it shares no token with "sp lisbon" while the
    # neighbouring key is "sp braga", so guessing off the word "Sporting" could
    # put Lisbon's markets onto Braga's ratings -- the Espanyol/Barcelona shape.
    # The caution was right; the conclusion is now settled by data rather than by
    # guessing. "Sp Lisbon" and "Sp Braga" appear on OPPOSITE SIDES of 63 P1
    # fixtures, most recently 2026-03-07, so they are certainly two clubs, and
    # both are in the current 18-team season alongside each other. Sp Lisbon is
    # the deepest-rated club in the league at 1,028 matches. Leaving the biggest
    # club in Portugal unpriced was the more expensive error of the two.
    "sporting cp": "sp lisbon",
    #
    # STILL DELIBERATELY NOT MAPPED:
    #   "Viseu" -> Academico de Viseu, a SECOND-TIER club. We only ingest P1, so
    #              there is no rating to map it to and there should not be one.
    # Left unresolved and visible (it prices as "no baseline") rather than being
    # attached to whichever top-flight key looks closest.
    #
    # ---- E1 + P1, 2026-08-07 (second pass) -----------------------------------
    # Adding Polymarket coverage for these two leagues made the cross-platform
    # fixture alignment available for them, which it was NOT a few hours earlier
    # -- the exact evidence source the P1 block above says was missing. Re-ran
    # scripts/derive_soccer_team_aliases.py and it pinned 58 name observations.
    #
    # Two of these could not have been guessed safely from the strings alone:
    #   vitoria sc  -> guimaraes  (Vitoria SC is Vitoria de Guimaraes)
    #   sc braga    -> sp braga
    #
    # CORRECTED 2026-08-07. This block originally shipped `cs maritimo ->
    # madeira`, on the grounds that the cross-platform fixture alignment
    # OVERRULED the obvious token match to the "maritimo" key. The alignment was
    # wrong and the obvious answer was right: Madeira and Maritimo are two
    # different clubs that PLAYED EACH OTHER TWICE in the 1994-95 Primeira Liga
    # (Maritimo 1-0 Madeira on 1995-01-08, Madeira 2-2 Maritimo on 1995-05-21).
    # "Madeira" is Uniao da Madeira, 34 matches in that one season; "Maritimo"
    # is CS Maritimo, 924 matches from 1994 to 2023. The alias sent every CS
    # Maritimo market to a 31-year-old rating belonging to a different club.
    #
    # THE GENERAL LESSON, which is why this is written out: fixture alignment is
    # evidence, not proof, and it is exactly as capable of being wrong as the
    # string heuristic it was introduced to overrule (it was introduced to stop
    # `rcd espanyol de barcelona -> barcelona`). A head-to-head existence check
    # is strictly stronger than either: two names that appear on OPPOSITE SIDES
    # of the same fixture cannot be the same club, and that is a fact about the
    # data rather than a judgement about the names. Run that check before
    # trusting alignment to overrule a token match.
    "sport lisboa e benfica": "benfica",
    "cf estrela da amadora": "estrela",
    "cs maritimo": "maritimo",
    "sc braga": "sp braga",
    "vitoria sc": "guimaraes",
    "cd nacional": "nacional",
    "cd santa clara": "santa clara",
    "casa pia ac": "casa pia",
    "estoril praia": "estoril",
    "fc alverca": "alverca",
    "fc arouca": "arouca",
    "fc famalicao": "famalicao",
    "fc porto": "porto",
    "gil vicente fc": "gil vicente",
    "moreirense fc": "moreirense",
    "rio ave fc": "rio ave",
    # E1: Polymarket suffixes every club with FC/AFC.
    # QPR is the one E1 club whose football-data key is an ABBREVIATION rather
    # than a shortened name, so stripping the "FC" suffix (which is all the rest
    # of this block does) still misses it. 1,050 E1 matches through 2026-05-02.
    "queens park rangers fc": "qpr",
    "blackburn rovers fc": "blackburn",
    "bristol city fc": "bristol city",
    "burnley fc": "burnley",
    "middlesbrough fc": "middlesbrough",
    "millwall fc": "millwall",
    "portsmouth fc": "portsmouth",
    "sheffield united fc": "sheffield united",
    "southampton fc": "southampton",
    "watford fc": "watford",
    "west bromwich albion fc": "west bromwich albion",
    "west ham united fc": "west ham united",
    "wolverhampton wanderers fc": "wolverhampton wanderers",
    "wrexham afc": "wrexham",
    # These nine came back on SUBSET evidence only, which the script holds back
    # by default because that is the tier that once proposed
    # "rcd espanyol de barcelona" -> "barcelona". Accepted here after checking
    # the specific failure mode does not apply: every one is the single English
    # pattern "<Name> City/County/North End" -> "<Name>", each has exactly one
    # candidate key, and crucially NONE of them contains another club's whole
    # name the way Espanyol's contains Barcelona's.
    "birmingham city fc": "birmingham",
    "bolton wanderers fc": "bolton",
    "cardiff city fc": "cardiff",
    "charlton athletic fc": "charlton",
    "derby county fc": "derby",
    "norwich city fc": "norwich",
    "preston north end fc": "preston",
    "stoke city fc": "stoke",
    "swansea city afc": "swansea",
    #
    # ---- N1 (Eredivisie), 2026-08-07 ----------------------------------------
    # Kalshi names several Dutch clubs by their CITY, which football-data names
    # by the club, so six of the eighteen share no usable token. Rather than
    # lean on knowing Dutch geography, these were read off KALSHI'S OWN TICKER
    # CODES, which encode the club independently of the display name:
    #
    #   KXEREDIVISIE-27-TWE -> "Enschede"   => Twente  (the one with NO shared
    #                                          token at all; FC Twente plays in
    #                                          Enschede, and Kalshi codes it TWE)
    #   KXEREDIVISIE-27-AZA -> "Alkmaar"    => AZ Alkmaar
    #   KXEREDIVISIE-27-PSV -> "Eindhoven"  => PSV Eindhoven
    #   KXEREDIVISIE-27-FOR -> "Sittard"    => For Sittard
    #   KXEREDIVISIE-27-GAE -> "GA Eagles"  => Go Ahead Eagles
    #   KXEREDIVISIE-27-SPA -> "Sparta"     => Sparta Rotterdam (see below)
    #
    # SPARTA IS THE DANGEROUS ONE and the reason this block spells its evidence
    # out. football-data carries BOTH "Sparta" (473 matches, 1993-08-14 to
    # 2010-05-02) and "Sparta Rotterdam" (298 matches, 2016-08-07 to
    # 2026-05-17). They are one club under two spellings from different eras --
    # they NEVER met (0 head-to-head fixtures) and their spans do not overlap,
    # the same disproof used for Maritimo/Madeira, run the other way. Without an
    # alias Kalshi's "Sparta" normalises straight onto the STALE 1993-2010 key
    # and every Sparta Rotterdam market would price off a 16-year-old rating.
    # ("Roda JC" 1993-2010 / "Roda" 2010-2018 is the same split; both are long
    # out of the division, so it is recorded here rather than aliased.)
    #
    # THE ALIAS ALSO MERGES THE TWO KEYS IN THE RATING POOL, which is worth
    # stating because it is easy to assume otherwise. elo_service_soccer's
    # refresh_ratings() runs canonical_team_key over the TRAINING matches, not
    # just over market names, so aliasing "sparta" here folds football-data's
    # own historical "Sparta" rows onto "sparta rotterdam" as well: the key goes
    # from 298 matches to 771. Verified by counting after the change.
    #
    # That merge is defensible -- it is genuinely one club, and the alternative
    # (Kalshi's "Sparta" landing on the stale 1993-2010 key) is plainly wrong --
    # but it IS a change to how that rating is built, and it carries a six-year
    # second-tier gap in the middle. Sparta Rotterdam's rating should be treated
    # as the least trustworthy in the N1 pool until it has been settled against.
    # There is no way to alias the market name without also merging the history
    # while the alias table is global.
    #
    # Ajax / Excelsior / Feyenoord / Groningen / Heerenveen / Nijmegen /
    # Telstar / Utrecht / Zwolle / Willem II / Cambuur / Den Haag already match
    # exactly and need no entry.
    "alkmaar": "az alkmaar",
    "eindhoven": "psv eindhoven",
    "enschede": "twente",
    "sittard": "for sittard",
    "ga eagles": "go ahead eagles",
    "sparta": "sparta rotterdam",
    # ---- Brazil / Argentina / Mexico / Japan (2026-08-08) --------------------
    # DERIVED, NOT TYPED. Every entry below comes from
    # scripts/build_soccer_kalshi_aliases.py, which infers a Kalshi name only
    # by joining real FIXTURES (date + club pair) against ESPN, never by
    # comparing strings. Its output was 46 aliases with 0 unresolved.
    #
    # WHY THIS BATCH EXISTS. The four extra-format leagues were wired up and
    # then priced badly or not at all -- BRA1 64 of 163, ARG1 22 of 78,
    # MEX1 9 of 36, and JAPAN 0 of 36. The clubs that DID price were simply the
    # ones Kalshi happens to spell exactly as football-data does (Palmeiras,
    # Corinthians, Boca Juniors, River Plate). Everything else was invisible.
    # The J-League was a total blank because Kalshi lists NICKNAMES that share
    # no token with football-data's names at all: "Frontale" for Kawasaki
    # Frontale, "Marinos" for Yokohama F. Marinos, "V-Varen" for V-Varen
    # Nagasaki.
    #
    # WHY THE BUILDER NEEDED A SECOND STAGE FOR JAPAN. Its original join
    # anchors on one ALREADY-RATED side and reads the opponent off the unique
    # ESPN fixture. That cannot bootstrap a league where NO club is known yet,
    # which is why Japan produced nothing on the first pass. Stage 2 proposes
    # candidates by token-prefix and then makes the FIXTURE decide, keeping an
    # assignment only when exactly one ESPN fixture within +/-1 day pairs a
    # candidate-for-A against a candidate-for-B. The string only narrows the
    # search; it never settles anything.
    #
    # THE PROOF THAT THIS IS NOT FUZZY MATCHING: "Tokyo" and "Tokyo V" resolve
    # SEPARATELY and correctly, to "fc tokyo" and "verdy" -- two different real
    # clubs in one city whose names differ by a single character. Similarity
    # scoring collapses that pair; the fixture join does not. (Same reason the
    # earlier Rangers->Angers 0.92 and Espanyol->Barcelona errors cannot recur.)
    #
    # AMBIGUITY WAS CHECKED, NOT ASSUMED. The alias table is GLOBAL, so a bare
    # name that means different clubs in different countries would misroute --
    # "America" is Club America in Mexico but there is also America-MG in
    # Brazil, and "Botafogo" is Botafogo-RJ here but Botafogo-SP also exists.
    # Every key below was swept against live Kalshi listings across all soccer
    # series: each appears under EXACTLY ONE division, none ambiguous. Kalshi
    # spells the Brazilian club "America MG", which normalizes distinctly and
    # is unaffected. Re-run that sweep if a promotion brings a colliding club
    # into a rated division.
    "argentinos juniors": "argentinos jrs",     # ARG1
    "barracas": "barracas central",             # ARG1
    "belgrano de cordoba": "belgrano",          # ARG1
    "estudiantes la plata": "estudiantes lp",   # ARG1
    "gimnasia la plata": "gimnasia lp",         # ARG1
    "independiente avellaneda": "independiente",  # ARG1
    "instituto cordoba": "instituto",           # ARG1
    "mendoza": "gimnasia mendoza",              # ARG1
    "racing avellaneda": "racing club",         # ARG1
    "riestra": "dep riestra",                   # ARG1
    "rivadavia": "ind rivadavia",               # ARG1
    "san lorenzo de almagro": "san lorenzo",    # ARG1
    "union santa fe": "union de santa fe",      # ARG1
    "atletico mineiro": "atleticomg",           # BRA1
    "botafogo": "botafogo rj",                  # BRA1
    "chapecoense": "chapecoensesc",             # BRA1
    "flamengo": "flamengo rj",                  # BRA1
    "paranaense": "athleticopr",                # BRA1
    "vasco da gama": "vasco",                   # BRA1
    "america": "club america",                  # MEX1
    "leon": "club leon",                        # MEX1
    "pumas unam": "unam pumas",                 # MEX1
    "san luis": "atl san luis",                 # MEX1
    "tigres": "tigres uanl",                    # MEX1
    "tijuana de caliente": "club tijuana",      # MEX1
    "avispa": "avispa fukuoka",                 # JPN1
    "cerezo": "cerezo osaka",                   # JPN1
    "fagiano o": "okayama",                     # JPN1
    "frontale": "kawasaki frontale",            # JPN1
    "hiroshima": "sanfrecce hiroshima",         # JPN1
    "kashima": "kashima antlers",               # JPN1
    "kashiwa": "kashiwa reysol",                # JPN1
    "kobe": "vissel kobe",                      # JPN1
    "kyoto sanga": "kyoto",                     # JPN1
    "marinos": "yokohama f marinos",            # JPN1
    "nagoya": "nagoya grampus",                 # JPN1
    "shimizu": "shimizu spulse",                # JPN1
    "tokyo": "fc tokyo",                        # JPN1
    "tokyo v": "verdy",                         # JPN1
    "urawa": "urawa reds",                      # JPN1
    "vvaren": "vvaren nagasaki",                # JPN1
    # These last two are held to a WEAKER standard than everything above, and
    # the difference is recorded rather than hidden. No fixture join can reach
    # them: both were listed against Mito HollyHock and JEF United Chiba, which
    # are J2 clubs absent from football-data's J1 file, so there is no rated
    # opponent to anchor on and no ESPN j1 fixture to match. What was checked
    # instead is FORCED UNIQUENESS -- each name is token-prefix compatible with
    # exactly ONE club out of all 1,245 rated across all 23 leagues, so there is
    # no second candidate to have chosen wrongly between. The same probe returns
    # ZERO candidates for "Mito H" and "United Chiba", which is the correct
    # answer for them: they are genuinely unrateable and stay refused rather
    # than being invented.
    "gamba": "gamba osaka",                     # JPN1
    "machida z": "machida",                     # JPN1
    # ---- Sweden / Norway / Denmark / China (2026-08-08) ---------------------
    # Fourth batch through build_soccer_kalshi_aliases.py, all fixture-derived.
    # Two recurring shapes: a suffix football-data drops ("BK Hacken" -> hacken,
    # "Dalian Yingbo FC" -> dalian yingbo) and Kalshi's ASCII transliteration of
    # a Scandinavian vowel ("Tromsoe" -> tromso, "Lillestroem" -> lillestrom,
    # "Vasteraas" -> vasteras sk), which no accent-folding rule catches because
    # the letter is already spelled out.
    #
    # "Shenzhen Peng City" -> shenzhen xinpengcheng is a real club RENAME, not a
    # spelling variant, and is exactly the kind of mapping that cannot be
    # derived from the strings at all -- only a shared fixture connects them.
    "chongqing tonglianglong fc": "chongqing tonglianglong",  # CHN1
    "dalian yingbo fc": "dalian yingbo",        # CHN1
    "henan": "henan songshan longmen",          # CHN1
    "qingdao west coast fc": "qingdao west coast",  # CHN1
    "shenzhen peng city": "shenzhen xinpengcheng",  # CHN1
    "wuhan three towns fc": "wuhan three towns",    # CHN1
    "zhejiang prof": "zhejiang professional",   # CHN1
    # ---- Leagues Cup (2026-08-08) ------------------------------------------
    # THE ONE GENUINE CROSS-COUNTRY NAME COLLISION found so far, and worth
    # spelling out because the collision sweep that clears every other alias
    # does NOT clear this one. Kalshi's Leagues Cup "Guadalajara" is Chivas of
    # Mexico, but the bare key "guadalajara" is already owned in the rated pool
    # by CD Guadalajara of SPAIN, which is why resolve_league returned SP2 and
    # the fixture was refused rather than priced. Refusing was correct: pricing
    # a Mexican club off a Spanish second-division rating is the exact class of
    # error this project keeps guarding against.
    #
    # The alias is still safe to add, because of how it FAILS. TEAM_ALIASES is
    # global, so a Spanish Segunda listing for "Guadalajara" would also be sent
    # to "guadalajara chivas" -- but that key does not exist in the SP2 pool, so
    # such a row would be REFUSED, not mispriced. A refusal is recoverable; a
    # wrong rating staked with real money is not.
    #
    # Checked before adding: no live SP2 listing normalizes to "guadalajara"
    # (CD Guadalajara is well below Segunda now, and the pool entry is
    # historical). Revisit if they are ever promoted back.
    "guadalajara": "guadalajara chivas",        # MEX1 (Leagues Cup)
    "randers": "randers fc",                    # DNK1
    # "Copenhagen" is forced -- exactly one candidate in the entire 26-league
    # pool. "Broendby" is the transliteration case again and NO token rule
    # reaches it, since "broendby" is not a prefix of "brondby"; it was verified
    # against the fixture instead (Kalshi "Horsens vs Broendby" on 2026-08-09
    # matches exactly one ESPN den.1 tie, "AC Horsens vs Brondby IF").
    "copenhagen": "fc copenhagen",              # DNK1
    "broendby": "brondby",                      # DNK1
    "lillestroem": "lillestrom",                # NOR1
    "sarpsborg": "sarpsborg 08",                # NOR1
    "tromsoe": "tromso",                        # NOR1
    "bk hacken": "hacken",                      # SWE1
    "malmo": "malmo ff",                        # SWE1
    "vasteraas": "vasteras sk",                 # SWE1
    # ---- Rated-but-never-listed leagues, wired 2026-08-08 -------------------
    # B1/D2/E2/SP2/T1/SC0 had ratings all along (added for cups and UEFA) but no
    # Kalshi series, so 297 open markets sat unpriced. 61% of their fixtures
    # resolved unaided; these close the rest, all fixture-derived.
    #
    # "Real Sociedad B" -> sociedad b is the entry that shows why the join is
    # worth the trouble. Segunda contains RESERVE sides, and Kalshi lists them
    # under names token-compatible with their first teams -- any name-similarity
    # rule maps them to Real Sociedad and stakes money on the wrong club in the
    # wrong division. The fixture sent it to the reserve club instead. Kalshi's
    # "Celta Fortuna" (Celta Vigo's reserves) is deliberately ABSENT for the
    # same reason inverted: no fixture resolved it, so it stays refused rather
    # than guessed onto Celta.
    "junin": "sarmiento junin",                 # ARG1
    "rosario": "rosario central",               # ARG1
    "la louviere": "raal la louviere",          # B1
    # Verified by hand against the same join the builder uses, which missed it
    # only for ordering reasons: the anchor needs one ALREADY-resolved side, and
    # this fixture's other side ("Zulte Waregem") was itself still unresolved on
    # that pass. Kalshi "Union Gilloise vs Zulte Waregem" on 2026-08-15 matches
    # exactly ONE ESPN bel.1 fixture that day, "Union St.-Gilloise vs
    # Zulte-Waregem". Re-running the builder now would derive it unaided.
    "union gilloise": "st gilloise",            # B1
    "leuven": "oudheverlee leuven",             # B1
    "royal antwerp": "antwerp",                 # B1
    "royal charleroi": "charleroi",             # B1
    "st truidense": "st truiden",               # B1
    "zulte waregem": "waregem",                 # B1
    "kiel": "holstein kiel",                    # D2
    "nuremberg": "nurnberg",                    # D2
    "sheffield wednesday": "sheffield weds",    # E0
    "milton keynes": "milton keynes dons",      # E1
    "notts": "notts county",                    # E1
    "oxford united": "oxford",                  # E1
    "peterborough": "peterboro",                # E1
    "heart of midlothian": "hearts",            # SC0
    "real sociedad b": "sociedad b",            # SP2
    "basaksehir": "buyuksehyr",                 # T1
    "kocaeli": "kocaelispor",                   # T1
    # ---- Gaps in LONG-SHIPPED European leagues (2026-08-08) -----------------
    # Found by sweeping EVERY live Kalshi soccer fixture through the production
    # resolver, which had never been done -- previous checks only looked at
    # leagues being added. These three had been silently unpriceable the whole
    # time, and two of them are first-division regulars, not obscurities.
    #
    # "Bilbao" is the one that justifies the whole fixture-join apparatus. It is
    # token-compatible with TWO rated clubs, "ath bilbao" (SP1) and the reserve
    # side "ath bilbao b" (SP2), so no uniqueness rule can settle it and any
    # reasoning about which one Kalshi "obviously" means is exactly the kind of
    # plausible guess that has misfired here before. A real La Liga fixture
    # picked the first team; nothing was assumed.
    "bilbao": "ath bilbao",                     # SP1
    "nottingham": "nottingham forest",          # E0
    "west bromwich": "west bromwich albion",    # E1
    # NOT added: Liga Portugal's "Viseu". Academico de Viseu is a second-tier
    # club and football-data's P1 file does not carry it, so it has no rating to
    # map onto -- the same correct refusal as Japan's Mito HollyHock. Adding
    # Liga Portugal 2 is the only thing that would fix it.
    # Cup-market gaps the same builder found in already-rated divisions. These
    # are why Coppa Italia / DFB Pokal ties read as "unrateable": the second-tier
    # club was rated all along, under a different spelling.
    "hellas verona": "verona",                  # I1
    "entella": "virtus entella",                # I2
    "stabia": "juve stabia",                    # I2
    "sudtirol bolzano": "sudtirol",             # I2
    "munster": "preuen munster",                # D2
    "rostock": "hansa rostock",                 # D2
}


# ---- data/soccer_kalshi_aliases.json, loaded rather than transcribed --------
#
# WHY THIS EXISTS (2026-08-18). Every "DERIVED, NOT TYPED" batch above was
# produced by scripts/build_soccer_kalshi_aliases.py and then COPIED IN BY HAND.
# The builder writes data/soccer_kalshi_aliases.json, and until now nothing in
# the app read that file -- its only reader was check_cup_market_coverage.py.
#
# That was caught the hard way. Seven leagues were wired on 2026-08-18, the
# builder was re-run and produced 70 verified aliases, the backend was
# restarted, and pricing did not move: 21 of 212 rows before, 21 of 212 after.
# The aliases were sitting in a file the app never opened. Same shape as the
# duplicate-listing cap that was wired to 4 of 13 routers -- the artifact
# existed, it just was not CONNECTED.
#
# So the file is now the source and this dict is the override. Hand-written
# entries WIN on conflict, because a few of them encode judgement the builder
# cannot reach (the Leagues Cup "Guadalajara" collision above being the
# example), and those must not be silently replaced by a re-run.
def _load_kalshi_aliases() -> dict[str, str]:
    path = _Path(__file__).resolve().parents[3] / "data" / "soccer_kalshi_aliases.json"
    try:
        raw = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # LOUD. A silent {} here means every league whose Kalshi spelling
        # differs from football-data's quietly stops pricing, and the only
        # symptom is rows that read "no tracked match history" -- which looks
        # like a coverage gap, not a broken file read.
        _log.error("soccer kalshi aliases unreadable at %s (%s) -- clubs whose "
                   "Kalshi spelling differs will price as unrated", path, exc)
        return {}
    out: dict[str, str] = {}
    for kalshi_name, entry in raw.items():
        key = normalize_team_name(kalshi_name) or ""
        target = normalize_team_name(entry.get("team") or "") or ""
        # A key that already normalizes onto its target teaches nothing, and an
        # entry that would shadow a hand-written one is skipped, not merged.
        if not key or not target or key == target or key in TEAM_ALIASES:
            continue
        out[key] = target
    return out


_FROM_FILE = _load_kalshi_aliases()
TEAM_ALIASES.update(_FROM_FILE)


def canonical_team_key(name: str) -> str:
    """Alias-normalized canonical key for a team name -- used both here (to
    match a live listing against an existing SoccerMatch row) AND by
    elo_service_soccer.py (to look up/train a team's rating), so the SAME
    real club always hits the SAME rating-dict key regardless of which
    platform's own spelling produced it. REAL BUG this fixes (caught live
    2026-07-19, this app's own first end-to-end poller run): elo_service_soccer
    previously did a raw exact-string dict lookup with no canonicalization at
    all -- ESPN's own training data says "Houston Dynamo FC", but a live
    Polymarket listing says "Houston Dynamo" (no "FC"), so EVERY MLS team
    whose platform-rendered name didn't byte-for-byte match ESPN's own name
    silently looked like a 0-history team (falling into the NO_HISTORY_REASON
    gate) even though real training data existed for it."""
    normalized = normalize_team_name(name) or ""
    return TEAM_ALIASES.get(normalized, normalized)


def team_names_match(name_a: str, name_b: str) -> bool:
    """Alias-normalized token-subset match -- same subset shape as
    market_matcher_tennis.py::full_names_match, but through the alias table
    first so e.g. "Man United" (football-data.co.uk) and "Manchester United"
    (a live Kalshi/Polymarket listing) resolve to the identical canonical
    string before comparing tokens at all."""
    canon_a, canon_b = canonical_team_key(name_a), canonical_team_key(name_b)
    if not canon_a or not canon_b:
        return False
    if canon_a == canon_b:
        return True
    tokens_a, tokens_b = set(canon_a.split()), set(canon_b.split())
    return tokens_a.issubset(tokens_b) or tokens_b.issubset(tokens_a)


def _match_pair(match: dict, home_name: str, away_name: str) -> bool:
    return team_names_match(home_name, match["home_team"]) and team_names_match(away_name, match["away_team"])


def match_upcoming_soccer_match(
    home_team_name: str, away_team_name: str, upcoming_matches: list[dict],
) -> dict | None:
    """upcoming_matches: SoccerMatch-shaped dicts (real name fields, not yet
    played) -- see app/ingestion/market_catalog_soccer.py for how these get
    populated live. Home/away order is meaningful here (unlike Tennis's
    player_a/player_b, which has no home-field concept) -- a swapped-order
    match is NOT accepted, since that would silently mislabel which side
    gets the real home-advantage rating bump."""
    if not home_team_name or not away_team_name:
        return None
    for match in upcoming_matches:
        if _match_pair(match, home_team_name, away_team_name):
            return match
    return None


# Kalshi's own per-league series-ticker prefix (confirmed live 2026-07-19,
# see market_catalog_soccer.py's kickoff audit) -> this app's
# SoccerMatch.league value (football-data.co.uk's division code, or "MLS").
#
# REAL BUG this fixes (caught live 2026-07-19, same day, while auditing the
# whole catalog for missing market types): this dict used to be a HAND-
# MAINTAINED, hardcoded GAME/SPREAD/TOTAL-only list -- every per-match
# series type added to kalshi_soccer_client.py AFTER that (BTTS first, then
# a much larger second batch: First Half/Second Half Winner/Spread/Total/
# BTTS, FTTS, Correct Score, Team Total) never got a matching entry here,
# so kalshi_match_suffix() silently returned None for every one of those
# series' real tickers -- confirmed live: EVERY tracked KXMLSBTTS market in
# the DB had soccer_match_id=NULL, meaning BTTS has been completely
# unmodeled (no match, no team names, no model_prob) since it shipped,
# without ever throwing an error or showing up as obviously broken. Rebuilt
# PROGRAMMATICALLY from kalshi_soccer_client.py's own per-market-type SERIES
# dicts instead of a second hand-maintained list, so this exact bug class
# (a new series type added to the client but never mirrored here) cannot
# recur -- adding a market type to the client's own SERIES dict is now the
# only thing needed for its matches to resolve here too.
def _build_prefix_to_division() -> dict[str, str]:
    from app.clients import kalshi_soccer_client as _kc

    series_dicts = [
        _kc.MONEYLINE_SERIES, _kc.SPREAD_SERIES, _kc.TOTAL_SERIES, _kc.BTTS_SERIES,
        _kc.FIRST_HALF_SERIES, _kc.FIRST_HALF_SPREAD_SERIES, _kc.FIRST_HALF_TOTAL_SERIES, _kc.FIRST_HALF_BTTS_SERIES,
        _kc.SECOND_HALF_SERIES, _kc.SECOND_HALF_SPREAD_SERIES, _kc.SECOND_HALF_TOTAL_SERIES, _kc.SECOND_HALF_BTTS_SERIES,
        _kc.FTTS_SERIES, _kc.SCORE_SERIES, _kc.TEAMTOTAL_SERIES,
    ]
    out = {}
    for series_dict in series_dicts:
        for division, ticker_prefix in series_dict.items():
            out[f"{ticker_prefix}-"] = division
    return out


_KALSHI_SOCCER_PREFIX_TO_DIVISION = _build_prefix_to_division()


def kalshi_match_suffix(event_ticker: str) -> tuple[str, str] | None:
    """"KXEPLGAME-26MAY24LFCBRE" -> ("E0", "26MAY24LFCBRE"). Same date+team-
    code suffix is shared across a league's GAME/SPREAD/TOTAL series for the
    same real match (confirmed live -- KXEPLGAME-26MAY24LFCBRE-TIE and any
    matching KXEPLSPREAD-26MAY24LFCBRE-... market share the identical
    suffix), same "cross-series join key" role as
    market_matcher_tennis.py::kalshi_match_suffix. Returns (division_code,
    suffix) so the caller doesn't need its own separate league-code lookup."""
    for prefix, division in _KALSHI_SOCCER_PREFIX_TO_DIVISION.items():
        if event_ticker.startswith(prefix):
            return division, event_ticker[len(prefix):]
    return None
