"""Competitive international (national-team) results, cached, in the same shape
soccer_data.load_matches() already yields so the rating pool can treat them as
one more league.

WHY A LEAGUE AND NOT A NEW SERVICE. elo_service_soccer.refresh_ratings() groups
matches by their `league` field and builds one SoccerRatingState per group.
Feeding these in tagged "INTL" therefore gets ratings, resolve_league, match
distributions and every downstream pricing path for free, with no parallel
service to keep in step. National teams genuinely ARE a self-contained pool --
they only ever play each other -- so the per-league assumption that makes club
ratings incomparable across leagues is exactly right here rather than a
compromise.

FRIENDLIES ARE EXCLUDED, DELIBERATELY, AND THIS IS THE EXPENSIVE CHOICE.
ESPN's fifa.friendly carries 460 completed matches -- far more than any single
competitive source below, and the obvious way to thicken a thin pool. It is
refused because check_club_friendlies_signal.py MEASURED what friendlies do to
this model: on 189 club friendlies the model was WORSE than a knows-nothing
league-average baseline at predicting goals (deviance gain -0.0152, against
+0.2548 on competitive matches), because rotating squads and no incentive to
chase a result break the mapping from rating to goals. International friendlies
are the same event shape and arguably worse, existing largely to try players
out. Training on matches the model cannot predict would corrupt the ratings for
the competitive fixtures actually being traded.

COVERAGE, measured 2026-08-09: 1,439 completed competitive matches yield 211
national teams, 178 of them with 6+ matches. That is not an ASEAN-only pool --
it covers World Cup qualifying across all six confederations, the UEFA Nations
League, the Asian Cup and the Gold Cup, so it will already have ratings
whenever Kalshi lists any of those.
"""
from __future__ import annotations

import collections
import datetime
import json
import logging
from pathlib import Path

from app.clients.base import get_json

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
CACHE_PATH = DATA_DIR / "international_matches_cache.json"
INTL_LEAGUE = "INTL"

SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}"
              "/scoreboard?dates={a}-{b}&limit=500")
# Competitive only. A slug that 400s contributes nothing rather than raising --
# ESPN's naming is inconsistent across confederations and this list is allowed
# to contain hopeful entries.
COMPETITIVE_SLUGS = [
    "fifa.worldq.afc", "fifa.worldq.uefa", "fifa.worldq.concacaf",
    "fifa.worldq.conmebol", "fifa.worldq.caf", "fifa.worldq.ofc",
    "uefa.nations", "aff.championship", "afc.asian.cup", "concacaf.gold",
    "conmebol.america", "caf.nations",
]


def _windows(start_year: int, end_year: int):
    for y in range(start_year, end_year + 1):
        yield (f"{y}0101", f"{y}1231")


def build_cache(start_year: int = 2021) -> list[dict]:
    """Fetch every competitive international in range and write the cache.

    Chronological, because ratings are trained by walking the list forward --
    an out-of-order list would let a team's rating be informed by matches that
    had not happened yet."""
    end_year = datetime.date.today().year
    rows, seen = [], set()
    per_slug: collections.Counter = collections.Counter()
    for slug in COMPETITIVE_SLUGS:
        for a, b in _windows(start_year, end_year):
            try:
                data = get_json(SCOREBOARD.format(slug=slug, a=a, b=b))
            except Exception:
                continue
            for ev in data.get("events", []):
                if ev.get("id") in seen:
                    continue
                seen.add(ev.get("id"))
                try:
                    comp = ev["competitions"][0]
                    if not comp.get("status", {}).get("type", {}).get("completed"):
                        continue
                    home = away = None
                    for c in comp["competitors"]:
                        side = (c["team"]["displayName"], int(c["score"]))
                        if c["homeAway"] == "home":
                            home = side
                        else:
                            away = side
                    if not home or not away:
                        continue
                    date = str(ev.get("date"))[:10]
                    datetime.date.fromisoformat(date)
                except (KeyError, IndexError, ValueError, TypeError):
                    continue
                rows.append({
                    "source": "espn_intl",
                    "source_match_id": f"intl:{ev.get('id')}",
                    "league": INTL_LEAGUE,
                    "season": date[:4],
                    "match_date": date,
                    "home_team": home[0],
                    "away_team": away[0],
                    "home_goals_ft": home[1],
                    "away_goals_ft": away[1],
                    "result_ft": ("H" if home[1] > away[1] else
                                  "A" if home[1] < away[1] else "D"),
                    "competition": slug,
                })
                per_slug[slug] += 1
    rows.sort(key=lambda r: r["match_date"])
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(rows), encoding="utf-8")
    log.info("international cache built: %d matches across %d competitions",
             len(rows), len(per_slug))
    return rows


def cache_inputs() -> list:
    """The files load_matches() reads. Exported so soccer_data's memo key can
    include them -- rebuilding the INTL cache must invalidate the merged
    stream, not just this module's slice of it."""
    return [CACHE_PATH]


def load_matches() -> list[dict]:
    """Cached competitive internationals. Returns [] when the cache has never
    been built, so a missing cache degrades to 'no INTL ratings' rather than
    blocking every other league's refresh."""
    if not CACHE_PATH.exists():
        return []
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("international cache unreadable -- treating as empty")
        return []


# Which confederation each competition belongs to. Used to derive a team ->
# confederation map FROM THE DATA rather than typing one out: a team is assigned
# the confederation whose competitions it appears in most often.
COMPETITION_CONFEDERATION = {
    "fifa.worldq.afc": "AFC", "afc.asian.cup": "AFC", "aff.championship": "AFC",
    "fifa.worldq.uefa": "UEFA", "uefa.nations": "UEFA",
    "fifa.worldq.concacaf": "CONCACAF", "concacaf.gold": "CONCACAF",
    "fifa.worldq.conmebol": "CONMEBOL", "conmebol.america": "CONMEBOL",
    "fifa.worldq.caf": "CAF", "caf.nations": "CAF",
    "fifa.worldq.ofc": "OFC",
}


def confederation_by_team() -> dict:
    """team key -> confederation, derived from which competitions the team
    actually played in.

    WHY THIS IS NEEDED AT ALL. The INTL pool looks like one rating pool but is
    really six that barely touch. Confederation qualifying is closed -- CONMEBOL
    is a round-robin among ten strong sides, so Brazil never gets to farm goals
    against minnows, while AFC qualifying lets Vietnam do exactly that. With
    almost no matches connecting the groups, the goal-scaling between them was
    never pinned down, and the ratings show it: measured 2026-08-09, Brazil's
    attack came out at -0.005 and Argentina's +0.063, BELOW Vietnam's +0.190.

    Within a confederation the ratings are fine, because those teams play each
    other constantly. Across confederations they are not comparable, which is
    the same problem the fitted league-strength offsets solve for clubs -- and
    it cannot be solved the same way here, because the inter-confederation
    matches that would anchor it are almost all FRIENDLIES, which this pool
    deliberately excludes for being unpredictable.
    """
    from collections import Counter, defaultdict
    from app.ingestion.market_matcher_soccer import canonical_team_key

    votes = defaultdict(Counter)
    for m in load_matches():
        conf = COMPETITION_CONFEDERATION.get(m.get("competition"))
        if not conf:
            continue
        for side in ("home_team", "away_team"):
            votes[canonical_team_key(m[side])][conf] += 1
    return {t: c.most_common(1)[0][0] for t, c in votes.items() if c}
