"""How much of UEFA club competition could this app ACTUALLY price, even with a
perfect cross-league model? (Task #92.)

THE QUESTION BEHIND THE QUESTION. Task #92 assumes the blocker for UCL/UEL is
that soccer ratings are per-league, so an EPL team's attack rating isn't
comparable to a Ligue 1 team's. That is true and it IS a blocker. But it is the
SECOND blocker. The first is team COVERAGE: this app rates 13 leagues, of which
only 7 are European top flights (E0 SP1 I1 D1 F1 N1 P1). UEFA's competitions
draw from 50+ member associations. A cross-league normalization is worthless for
any match where one side is from a league that was never rated at all, because
the app's standing rule is to return None rather than invent a rating for an
unrated team.

So before designing any normalization, measure the ceiling: of real UEFA matches,
what fraction have BOTH teams already rated? That number caps everything a
cross-league model could unlock, and it is cheap to measure exactly.

METHOD. Sweep ESPN's free scoreboard for each UEFA club competition across a real
completed season, canonicalize both team names through the app's own
market_matcher_soccer.canonical_team_key (the same function the pricing path
uses -- checking membership any other way would measure a different thing than
production sees, which is the "rating-lookup vs pricing-path" gap this project
has already hit three times), and count matches by how many sides are rated.

===========================================================================
RESULT, 2026-08-08 (2025-26 season, 531 matches). Measured TWICE -- the first
number was wrong and the correction matters, so both are kept.

FIRST RUN, before aliases existed -- A FLOOR, NOT A CEILING:

    Champions League   both rated 27.0%
    Europa League      both rated 13.8%
    Conference League  both rated  6.5%
    ALL                both rated 16.4%

That 16.4% was contaminated by naming, and its own output gave it away: the most
frequent "unrated" clubs were Paris Saint-Germain, Bayer Leverkusen,
Internazionale, Borussia Dortmund, VfB Stuttgart and SC Freiburg -- all in
leagues this app already rates, just spelled differently by ESPN than by
football-data. Reporting 16.4% as the ceiling would have killed a viable project
on a measurement artifact.

SECOND RUN, with the 255 fixture-verified aliases from
build_soccer_espn_aliases.py -- THE REAL CEILING:

    Champions League   189 matches   both rated 105 = 55.6%   (was 27.0%)
    Europa League      189 matches   both rated  52 = 27.5%   (was 13.8%)
    Conference League  153 matches   both rated  10 =  6.5%   (unchanged)
    ALL                531 matches   both rated 167 = 31.5%   (was 16.4%)

So the Champions League is genuinely viable -- 105 priceable matches a season,
not 51 -- while the Conference League is hopeless and did not move at all,
because its field really is from uncovered leagues rather than misspelled ones.

THE ALIAS MAP COULD NOT HAVE BEEN FUZZY-MATCHED. check_uefa_name_gap.py's
similarity scorer produced Rangers -> Angers at 0.92 (Scotland -> France) and
Celtic -> Celta at 0.73 (Scotland -> Spain) beside correct hits like
AS Monaco -> monaco at 1.00; no threshold separates them. Same failure mode as
the Espanyol -> Barcelona match this project already caught, and either would
stake money on the wrong club. build_soccer_espn_aliases.py joins on FIXTURES
(date + exact score) instead, which is why Rangers and Celtic are correctly
absent from its output -- Scotland has no fixtures in this app to join against.

WHAT BLOCKED THE REMAINING 68% was clubs from leagues never rated. The seven
worst offenders were Racing Genk and Club Brugge (Belgium), Galatasaray and
Fenerbahce (Turkey), and Panathinaikos, Olympiacos and PAOK (Greece) -- all
three countries already published by football-data.co.uk on the same mmz4281
pattern this app has always used.

THIRD RUN, after adding B1/T1/G1 (2026-08-08, +25,297 matches, 16 leagues /
695 rated teams, 311 aliases):

    Champions League   189 matches   both rated 140 = 74.1%   (from 27.0 -> 55.6)
    Europa League      189 matches   both rated  73 = 38.6%   (from 13.8 -> 27.5)
    Conference League  153 matches   both rated  17 = 11.1%   (from  6.5 ->  6.5)
    ALL                531 matches   both rated 230 = 43.3%   (from 16.4 -> 31.5)

The Champions League is now comfortably viable: 140 of 189 matches priceable,
and only 5 (2.6%) with NEITHER side rated. Three league additions from a source
already wired bought +35 UCL matches a season.

STILL UNREACHED, and each needs a league football-data does not publish:
Bodo/Glimt (Norway), Ferencvaros (Hungary), Shakhtar Donetsk (Ukraine), Qarabag
(Azerbaijan). The Conference League remains structurally hopeless at 11.1% --
its field is drawn from exactly the associations no free source covers.

THE CHEAPER ADJACENT WIN. Kalshi lists Coppa Italia and DFB Pokal (GAME,
ADVANCE, TOTAL series, all live as of 2026-08-07). Those are Serie A vs Serie B
and Bundesliga vs 2. Bundesliga -- cross-DIVISION inside one country, where this
app already rates BOTH tiers (I1+I2, D1+D2), both sides come from the same
football-data feed so there is no alias problem at all, and season_sim_soccer
ALREADY carries a measured tier-1/tier-2 bridge: PROMOTED_TEAM_ATTACK_LOG_DISCOUNT
= -0.2558 and PROMOTED_TEAM_CONCEDE_LOG_DISCOUNT = +0.2444, derived from 476 real
promotion events with stdev ~0.20. That is a cross-league normalization that
already exists and is already validated -- just never pointed at cup fixtures.
===========================================================================
"""
from __future__ import annotations

import collections
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.base import get_json  # noqa: E402
from app.ingestion.market_matcher_soccer import canonical_team_key  # noqa: E402
from app.models.baseline import elo_service_soccer  # noqa: E402

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard?dates={d}"
COMPS = {
    "uefa.champions": "Champions League",
    "uefa.europa": "Europa League",
    "uefa.europa.conf": "Conference League",
}
# A full completed UEFA season: league phase through final.
START = datetime.date(2025, 9, 1)
END = datetime.date(2026, 6, 1)
# NOTE: ESPN 403s a custom urllib User-Agent but serves the app's shared httpx
# client fine, so this reuses get_json rather than hand-rolling a fetch -- the
# same reason every other client in this app goes through it.


ALIAS_PATH = Path(__file__).resolve().parents[2] / "data" / "soccer_espn_aliases.json"


def main() -> None:
    # WARM FIRST. A cold service reports every team unrated and would produce a
    # confident, completely wrong 0% answer.
    elo_service_soccer.refresh_ratings()
    states = elo_service_soccer._cache["states_by_league"]
    rated: dict[str, str] = {}
    for lg, st in states.items():
        for team in st.attack_log:
            rated.setdefault(team, lg)
    print(f"{len(rated)} rated teams across {len(states)} leagues: {sorted(states)}")

    # The FIXTURE-DERIVED alias map (build_soccer_espn_aliases.py). Without it
    # this audit reports a floor, not a ceiling -- ESPN and football-data spell
    # the same clubs differently, so PSG/Inter/Dortmund all read as "unrated".
    aliases: dict = {}
    if ALIAS_PATH.exists():
        aliases = json.loads(ALIAS_PATH.read_text(encoding="utf-8"))
    print(f"{len(aliases)} verified ESPN aliases loaded"
          f"{' -- RUN build_soccer_espn_aliases.py FIRST' if not aliases else ''}\n")

    def resolve(name: str) -> str | None:
        """ESPN club name -> a rated key, via direct canonicalization or a
        fixture-verified alias. Never guesses."""
        k = canonical_team_key(name)
        if k in rated:
            return k
        entry = aliases.get(name)
        if entry:
            k2 = canonical_team_key(entry["team"])
            if k2 in rated:
                return k2
        return None

    overall = collections.Counter()
    unrated_names: collections.Counter = collections.Counter()

    for comp, label in COMPS.items():
        seen: set[str] = set()
        counts = collections.Counter()
        pair_leagues: collections.Counter = collections.Counter()
        d = START
        while d <= END:
            url = SCOREBOARD.format(league=comp, d=d.strftime("%Y%m%d"))
            try:
                data = get_json(url)
            except Exception:
                d += datetime.timedelta(days=1)
                continue
            for ev in data.get("events", []):
                eid = ev.get("id")
                if eid in seen:
                    continue
                seen.add(eid)
                try:
                    cs = ev["competitions"][0]["competitors"]
                    names = [c["team"]["displayName"] for c in cs]
                except (KeyError, IndexError):
                    continue
                keys = [resolve(n) for n in names]
                n_rated = sum(k is not None for k in keys)
                counts[n_rated] += 1
                if n_rated == 2:
                    pair_leagues[tuple(sorted((rated[keys[0]], rated[keys[1]])))] += 1
                for n, k in zip(names, keys):
                    if k is None:
                        unrated_names[n] += 1
            d += datetime.timedelta(days=1)

        total = sum(counts.values())
        overall += counts
        if not total:
            print(f"{label}: no matches found\n")
            continue
        print(f"=== {label} === {total} matches {START} -> {END}")
        for n in (2, 1, 0):
            print(f"  {n} of 2 teams rated: {counts[n]:4d}  ({counts[n]/total:6.1%})")
        if pair_leagues:
            top = ", ".join(f"{a}v{b}:{c}" for (a, b), c in pair_leagues.most_common(6))
            print(f"  most common priceable pairings: {top}")
        print()

    total = sum(overall.values())
    if total:
        print(f"=== ALL UEFA CLUB COMPETITION === {total} matches")
        print(f"  BOTH teams rated (ceiling for a cross-league model): "
              f"{overall[2]} / {total} = {overall[2]/total:.1%}")
        print(f"  one side unrated: {overall[1]/total:.1%}   neither: {overall[0]/total:.1%}")
        print("\n  most frequent UNRATED clubs (each would need its league added):")
        for name, c in unrated_names.most_common(15):
            print(f"    {c:3d}  {name}")


if __name__ == "__main__":
    main()
