"""Split UEFA's "unrated" clubs into NAME MISMATCHES vs GENUINELY UNCOVERED.

WHY THIS EXISTS. check_uefa_coverage.py's first run said only 16.4% of UEFA
matches have both teams rated -- but its own list of most-frequent unrated clubs
was led by Paris Saint-Germain, Bayer Leverkusen, Internazionale, Borussia
Dortmund, VfB Stuttgart and SC Freiburg. Those are Ligue 1, Bundesliga and Serie
A clubs, and this app rates all three leagues. They are not uncovered; ESPN just
spells them differently than football-data.co.uk does ("Paris SG", "Leverkusen",
"Inter", "Dortmund", "Stuttgart", "Freiburg"). So 16.4% was a floor produced by
a naming gap, not the real ceiling, and reporting it would have understated the
opportunity badly. Same failure that left La Liga unpriced until 2026-08-06.

WHAT THIS DOES. For every distinct club appearing in UEFA competition that the
app cannot currently look up, propose the best candidate among rated teams using
token overlap, and print it for review WITHOUT applying anything automatically.

THE MATCHING IS DELIBERATELY NOT TRUSTED. This project has already been burned
by a unique token match that was still wrong (Espanyol -> Barcelona, via the
shared token "Barcelona" in "RCD Espanyol de Barcelona"). So candidates are
printed with their score and the leagues involved, split into a high-confidence
band and a needs-eyes band, and nothing is written to disk. The output is
evidence for a human decision, not a mapping.
"""
from __future__ import annotations

import collections
import datetime
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.base import get_json  # noqa: E402
from app.ingestion.market_matcher_soccer import canonical_team_key  # noqa: E402
from app.models.baseline import elo_service_soccer  # noqa: E402

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard?dates={d}"
COMPS = ["uefa.champions", "uefa.europa", "uefa.europa.conf"]
START = datetime.date(2025, 9, 1)
END = datetime.date(2026, 6, 1)

# Noise tokens that carry no identity -- matching on these is how you get
# Espanyol -> Barcelona.
STOP = {"fc", "cf", "sc", "ac", "as", "ss", "sv", "vfb", "vfl", "fk", "sk", "bk",
        "if", "il", "cd", "rc", "rcd", "ud", "cs", "us", "afc", "bsc", "tsg",
        "club", "de", "the", "1", "04", "05", "07", "1899", "1900", "de"}


def tokens(name: str) -> set[str]:
    raw = "".join(ch if ch.isalnum() else " " for ch in name.lower()).split()
    return {t for t in raw if t not in STOP}


def main() -> None:
    elo_service_soccer.refresh_ratings()
    states = elo_service_soccer._cache["states_by_league"]
    rated: dict[str, str] = {}
    for lg, st in states.items():
        for team in st.attack_log:
            rated.setdefault(team, lg)

    # Collect every distinct club that plays in UEFA.
    appearances: collections.Counter = collections.Counter()
    display: dict[str, str] = {}
    for comp in COMPS:
        d = START
        seen: set[str] = set()
        while d <= END:
            try:
                data = get_json(SCOREBOARD.format(league=comp, d=d.strftime("%Y%m%d")))
            except Exception:
                d += datetime.timedelta(days=1)
                continue
            for ev in data.get("events", []):
                if ev.get("id") in seen:
                    continue
                seen.add(ev.get("id"))
                try:
                    cs = ev["competitions"][0]["competitors"]
                except (KeyError, IndexError):
                    continue
                for c in cs:
                    nm = c["team"]["displayName"]
                    appearances[nm] += 1
                    display[canonical_team_key(nm)] = nm
            d += datetime.timedelta(days=1)

    missing = {k: n for k, n in ((canonical_team_key(nm), c) for nm, c in appearances.items())
               if k not in rated}
    print(f"{len(appearances)} distinct UEFA clubs, {len(missing)} not currently rateable\n")

    rated_tokens = {rk: tokens(rk) for rk in rated}
    strong, weak = [], []
    for key, count in sorted(missing.items(), key=lambda kv: -kv[1]):
        mt = tokens(key)
        best, best_score = None, 0.0
        for rk, rt in rated_tokens.items():
            if not mt or not rt:
                continue
            jac = len(mt & rt) / len(mt | rt)
            seq = difflib.SequenceMatcher(None, key, rk).ratio()
            score = max(jac, seq)
            if score > best_score:
                best, best_score = rk, score
        row = (count, display.get(key, key), best, rated.get(best, "?"), best_score)
        (strong if best_score >= 0.55 else weak).append(row)

    print("=== LIKELY NAME MISMATCH (league already rated) -- verify each ===")
    print(f"{'apps':>5} {'ESPN name':30s} -> {'rated team':26s} {'lg':4s} {'score':>6s}")
    for count, nm, best, lg, sc in strong:
        print(f"{count:5d} {nm[:30]:30s} -> {str(best)[:26]:26s} {lg:4s} {sc:6.2f}")

    print(f"\n=== NO PLAUSIBLE MATCH -- league genuinely not covered ({len(weak)}) ===")
    shown = [f"{nm} ({c})" for c, nm, *_ in weak[:40]]
    print("  " + "\n  ".join(shown))
    print(f"\nappearances: mismatch-band {sum(c for c, *_ in strong)}, "
          f"uncovered-band {sum(c for c, *_ in weak)}")


if __name__ == "__main__":
    main()
