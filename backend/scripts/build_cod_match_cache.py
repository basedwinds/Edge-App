"""Build data/cod_historical_match_cache.json -- Call of Duty League match
history from Liquipedia, for a CoD rating model.

WHY THE API AND NOT THE HTML, which is how CS2 was crawled. Liquipedia now
returns 403 to a plain page fetch. That is not a Call-of-Duty-specific block:
checked live 2026-08-09, the SAME client gets 403 on
liquipedia.net/counterstrike/S-Tier_Tournaments too, the exact page
build_cs2_match_cache.py scrapes. So the CS2 crawler can no longer refresh
either -- its cache still works because it is stored, not because the crawl
still runs. Worth knowing before anyone tries to re-run it.

The api.php route is unaffected and is what Liquipedia actually asks automated
clients to use, so this reads WIKITEXT and parses the match templates directly
rather than a rendered DOM.

WHAT THE WIKITEXT GIVES, verified on Call of Duty League/Season 7/Playoffs:

    {{Match
      |bestof=5
      |date=2026-07-16 17:30 {{abbr/MDT}}
      |opponent1={{TeamOpponent|tx}}
      |opponent2={{TeamOpponent|mia}}
      |map1={{Map|map=Hacienda|mode=hp|score1=250|score2=202|winner=1}}
      ...
      |map4={{Map|...|winner=skip}}

Series score is counted from the per-map `winner=` fields; `skip` marks maps
that were never played (a 3-0 in a Bo5), so counting them would invent maps that
do not exist.

TEAMS ARE SHORTCODES ("tx", "mia"), which no market will ever say. They resolve
through the API too -- {{TeamPage|tx}} expands to "OpTic Texas" -- and that
mapping is cached, because it is the join key every Kalshi and Polymarket row
will need.

RATE LIMITED ON PURPOSE, AND HARDER THAN YOU WOULD GUESS. Liquipedia's API
terms ask for a real User-Agent and throttled calls, and the parse action is
limited aggressively: 2.5s between requests was enough to earn an HTTP 429
partway through a ~70-page run. See DELAY below -- the failure mode is silent
and reads as "this wiki has no data", which is exactly how it presented.

Being rude here would get the whole project blocked from Liquipedia, which also
serves the CS2 pipeline, so the delay is deliberately conservative and the
crawl is resumable rather than fast.
"""
from __future__ import annotations

import datetime
import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
MATCH_CACHE = DATA_DIR / "cod_historical_match_cache.json"
TEAM_CACHE = DATA_DIR / "cod_team_shortcodes.json"

API = "https://liquipedia.net/callofduty/api.php"
UA = "nfl-edge-app/1.0 (personal research; contact via github.com/basedwinds/Edge-App)"
# 30s, not 2.5s. Liquipedia rate-limits the parse action far harder than a
# normal API: a 2.5s delay across ~70 pages earned an HTTP 429 partway through
# (confirmed live 2026-08-09 -- the run returned 0 matches not because the
# parser failed but because every fetch after the limit came back 429, and
# fetch_wikitext turns a non-200 into None).
#
# That failure is worth the comment because it is SILENT: a rate-limited crawl
# looks exactly like "this wiki has no data". The first run of this script
# printed "0 matches from 0 pages" and the parser was fine all along.
#
# A full crawl at this delay takes ~35-45 minutes. It is resumable -- both
# caches are read at startup and written at the end -- so it can be run in
# chunks, and a re-run costs nothing for pages already collected.
DELAY = 30.0

# The CDL is the only Call of Duty competition Kalshi and Polymarket list, so
# the crawl is scoped to it rather than every CoD event ever played. Season
# pages are enumerated rather than searched, because search returns player and
# org pages mixed in with tournaments.
SEASON_PAGES: list[str] = []
for _season in range(3, 9):  # Season 3 (2022) through Season 8, extended as they appear
    SEASON_PAGES.append(f"Call of Duty League/Season {_season}")
    SEASON_PAGES.append(f"Call of Duty League/Season {_season}/Playoffs")
    for _stage in range(1, 6):
        SEASON_PAGES.append(f"Call of Duty League/Season {_season}/Stage {_stage}")
        SEASON_PAGES.append(f"Call of Duty League/Season {_season}/Stage {_stage}/Major")
        SEASON_PAGES.append(f"Call of Duty League/Season {_season}/Stage {_stage}/Minor")

_client = httpx.Client(timeout=60.0, headers={"User-Agent": UA})

_MATCH_RE = re.compile(r"\{\{Match\b")
_DATE_RE = re.compile(r"\|date=\s*(\d{4}-\d{2}-\d{2})")
_BESTOF_RE = re.compile(r"\|bestof=\s*(\d+)")
_OPP_RE = re.compile(r"\|opponent(\d)=\s*\{\{TeamOpponent\|([^}|]+)")
_MAPWIN_RE = re.compile(r"\|map\d+=\s*\{\{Map\|[^}]*?\|winner=(\d|skip)")


def load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")


def fetch_wikitext(page: str) -> str | None:
    try:
        r = _client.get(API, params={"action": "parse", "page": page,
                                     "prop": "wikitext", "format": "json"})
    except httpx.HTTPError:
        return None
    finally:
        time.sleep(DELAY)
    if r.status_code != 200:
        return None
    return (r.json().get("parse", {}).get("wikitext", {}) or {}).get("*")


def _balanced_block(text: str, start: int) -> str:
    """The {{Match ...}} template body, brace-balanced. A naive scan to the next
    '}}' would stop inside the first nested {{Map}} and lose every later map."""
    depth, i = 0, start
    while i < len(text):
        if text.startswith("{{", i):
            depth += 1
            i += 2
            continue
        if text.startswith("}}", i):
            depth -= 1
            i += 2
            if depth == 0:
                return text[start:i]
            continue
        i += 1
    return text[start:]


def parse_matches(wikitext: str, event: str) -> list[dict]:
    rows = []
    for m in _MATCH_RE.finditer(wikitext):
        block = _balanced_block(wikitext, m.start())
        opps = dict((g, s.strip()) for g, s in _OPP_RE.findall(block))
        if "1" not in opps or "2" not in opps:
            continue  # a placeholder bracket slot with no teams yet
        d = _DATE_RE.search(block)
        if not d:
            continue  # undated rows cannot be ordered, and order is everything
        try:
            date = datetime.date.fromisoformat(d.group(1))
        except ValueError:
            continue
        wins = _MAPWIN_RE.findall(block)
        a = sum(1 for w in wins if w == "1")
        b = sum(1 for w in wins if w == "2")
        if a == 0 and b == 0:
            continue  # scheduled but unplayed
        bo = _BESTOF_RE.search(block)
        rows.append({
            "source": "liquipedia_cod",
            "source_match_id": f"cod:{event}:{date}:{opps['1']}:{opps['2']}",
            "event": event,
            "match_date": date.isoformat(),
            "team_a_code": opps["1"],
            "team_b_code": opps["2"],
            "score_a": a,
            "score_b": b,
            "best_of": int(bo.group(1)) if bo else None,
            "winner_code": opps["1"] if a > b else opps["2"] if b > a else None,
        })
    return rows


def resolve_team_names(codes: set[str], cache: dict) -> dict:
    """shortcode -> real team name via {{TeamPage|code}}. Cached, and batched a
    few per call so a full crawl does not need one request per team."""
    todo = sorted(c for c in codes if c not in cache)
    for i in range(0, len(todo), 12):
        chunk = todo[i:i + 12]
        text = "".join("{{TeamPage|%s}}\n" % c for c in chunk)
        try:
            r = _client.get(API, params={"action": "expandtemplates", "text": text,
                                         "prop": "wikitext", "format": "json"})
            out = r.json().get("expandtemplates", {}).get("wikitext", "")
        except Exception:
            out = ""
        finally:
            time.sleep(DELAY)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if len(lines) == len(chunk):
            for code, name in zip(chunk, lines):
                cache[code] = name
        else:
            # Length mismatch means the expansion did not line up with the
            # inputs; mapping them positionally anyway would silently attach the
            # wrong club to a rating. Fall back to one at a time.
            for code in chunk:
                try:
                    r = _client.get(API, params={"action": "expandtemplates",
                                                 "text": "{{TeamPage|%s}}" % code,
                                                 "prop": "wikitext", "format": "json"})
                    name = r.json().get("expandtemplates", {}).get("wikitext", "").strip()
                    if name and "{{" not in name:
                        cache[code] = name
                except Exception:
                    pass
                finally:
                    time.sleep(DELAY)
    return cache


def main() -> None:
    matches: dict[str, dict] = {m["source_match_id"]: m for m in load(MATCH_CACHE, [])}
    team_cache: dict = load(TEAM_CACHE, {})
    print(f"starting from {len(matches)} cached matches, {len(team_cache)} known teams")

    found_pages = 0
    for page in SEASON_PAGES:
        wt = fetch_wikitext(page)
        if not wt:
            continue
        rows = parse_matches(wt, page)
        if rows:
            found_pages += 1
            print(f"  {page:52s} {len(rows):3d} matches")
        for r in rows:
            matches[r["source_match_id"]] = r

    print(f"\n{len(matches)} matches from {found_pages} pages with content")
    codes = {m["team_a_code"] for m in matches.values()} | {m["team_b_code"] for m in matches.values()}
    print(f"resolving {len(codes)} team shortcodes...")
    team_cache = resolve_team_names(codes, team_cache)
    save(TEAM_CACHE, team_cache)

    unresolved = sorted(c for c in codes if c not in team_cache)
    for m in matches.values():
        m["team_a"] = team_cache.get(m["team_a_code"])
        m["team_b"] = team_cache.get(m["team_b_code"])
        m["winner"] = team_cache.get(m["winner_code"]) if m.get("winner_code") else None
    save(MATCH_CACHE, sorted(matches.values(), key=lambda r: r["match_date"]))

    print(f"resolved {len(team_cache)} teams, {len(unresolved)} unresolved: {unresolved[:8]}")
    print(f"wrote {MATCH_CACHE}")
    dated = sorted(m["match_date"] for m in matches.values())
    if dated:
        print(f"date range {dated[0]} .. {dated[-1]}")


if __name__ == "__main__":
    main()
