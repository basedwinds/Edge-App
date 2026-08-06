"""tennisexplorer.com client -- free, unauthenticated, no bot/Cloudflare gate
(confirmed live 2026-07-18: plain httpx + a browser User-Agent loads real
HTML tables, no PoW/JS challenge like ufcstats.com needs). This is the
source that closes the "zero Challenger/ITF training data" gap the user's
earlier standalone tennis-model project hit -- confirmed live: real
Challenger-level results (matching real currently-open Kalshi/Polymarket
matches, e.g. Cordenons/Granby) going back to at least 2018, WITH real
embedded bookmaker odds (`coursew`/`course` table cells) at Challenger AND
ITF level -- something tennis-data.co.uk never has at any tier below tour
level. tennis-data.co.uk (see tennisdata_client.py) remains the source for
ATP/WTA tour-level (cleaner structured xlsx, no scrape fragility, already
has point-in-time rank); this client is used ONLY for Challenger/ITF.

The tier signal is the TOURNAMENT SLUG itself: "-challenger" in the slug means
Challenger level, "-itf" means ITF level, anything else under a plain
`/atp-men/` or `/wta-women/` URL suffix is tour-level (already covered by
tennis-data.co.uk, skipped here to avoid double-counting/a second name-key
system for the same real matches).

CORRECTION 2026-08-06 -- this docstring used to claim, as a "real structural
finding", that `?type=` does NOT filter server-side, because `type=all`,
`type=challenger-men` and `type=itf-men` returned the same page. That
conclusion was wrong. The param DOES filter: `type=atp-single` returns a
genuinely different, men-only page (6 tour + 5 challenger tournaments, zero
WTA). What actually happened is that `challenger-men` and `itf-men` are not
valid values, and an invalid value silently falls back to the default page --
which looks identical to "the param is ignored". The values the site itself
links to are exactly: all, atp-single, atp-double, wta-single, wta-double,
double. There is no ITF filter among them.

`type=all` is still the right single request, but NOT because it returns every
tier -- because it returns the most. What it actually carries, measured on
2026-08-04: 6 ATP tour + 6 Challenger + 7 WTA tour + 28 ITF tournaments, and
every ITF one sits under a `/wta-women/` suffix.

KNOWN COVERAGE HOLE, measured not assumed: MEN'S ITF IS ABSENT. Of our own
itf/atp fixtures only 24 of 743 ever resolve (3%), against 636 of 802 (79%) for
itf/wta; restricted to dates inside the results lookback window it is 0 of 347.
It is not a labelling problem -- fetch_results_index keys purely on the player
pair, with no tier/tour in the key, so a men's row filed under `wta-women`
would still match. Those players simply are not on this page. Closing that gap
needs a different source, not a different parse of this one.

Known simplification, not yet fixed: this results-day page does NOT expose
per-tournament SURFACE (unlike the live "today's matches" listing page,
which shows a surface color swatch) -- surface would need a second request
per unique tournament slug (cheap: far fewer tournaments than matches, but
not built in this pass). Challenger/ITF rows from this source ship with
surface=None; tour-level rows keep tennis-data.co.uk's real Surface value
undisturbed. Flagged rather than guessed.
"""
from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.tennisexplorer.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def classify_tier(tourney_slug: str) -> str:
    if "-challenger" in tourney_slug:
        return "challenger"
    if "-itf" in tourney_slug:
        return "itf"
    return "tour"


def _tour_from_suffix(suffix: str) -> str | None:
    if suffix.startswith("atp-"):
        return "atp"
    if suffix.startswith("wta-"):
        return "wta"
    return None


class TennisExplorerClient:
    def __init__(self) -> None:
        self._client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TennisExplorerClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_tournament_draw(self, slug: str, year: int, tour_suffix: str) -> tuple[list[list[str]] | None, str | None]:
        """Real, freely-scrapable tournament bracket PLUS real surface
        (confirmed live 2026-07-19, e.g. /bastad/2026/atp-men/) -- used for
        tournament-winner futures (see bracket_sim_tennis.py). Both come off
        the SAME page (one request, not two) -- no bot gate, same as the
        rest of this client. Returns (rounds, surface); either half can be
        None independently (a tournament can have a surface listed with no
        draw yet, or vice versa)."""
        resp = self._client.get(f"{BASE_URL}/{slug}/{year}/{tour_suffix}/")
        return parse_draw_html(resp.text), parse_tournament_surface(resp.text)


    def get_scheduled_times(self) -> dict[frozenset, str]:
        """{frozenset({"Surname A.", "Surname B."}): ISO-UTC start} per match.

        WHY THIS EXISTS. Kalshi's occurrence_datetime is a scheduled estimate it
        NEVER revises, so as an order of play slips the app keeps showing the
        original time -- and every "has it started?" gate reasons from a wrong
        number. tennisexplorer tracks the real order of play. Verified live
        2026-08-03 against two matches the user confirmed were already under way:

            Izquierdo Luque vs Sanchez Quilez  -> 17:10Z  (Kalshi said 23:00Z)
            Zink vs Hurrion                    -> 16:50Z  (Kalshi said 19:00Z)

        TIMEZONE. The site renders Berlin time and applies DST on top of whatever
        my_timezone is set to -- even my_timezone=0 shows UTC+1 in summer, so no
        cookie value yields true UTC. Instead the page publishes its OWN current
        clock next to a timezone label, so the offset is derived from that on
        every fetch. That is DST-proof and survives the site changing its default.
        """
        import datetime
        import re as _re

        resp = self._client.get(f"{BASE_URL}/matches/", cookies={"my_timezone": "0"})
        html = resp.text
        now = datetime.datetime.now(datetime.timezone.utc)
        stamp = _re.search(r"(\d{2})\.(\d{2})\.\s*(\d{2}):(\d{2}),\s*<span class=\"timezone\"", html)
        if not stamp:
            return {}   # no clock to calibrate against -- better nothing than a wrong hour
        day, month, hh, mm = (int(x) for x in stamp.groups())
        try:
            page_now = datetime.datetime(now.year, month, day, hh, mm, tzinfo=datetime.timezone.utc)
        except ValueError:
            return {}
        offset = round((page_now - now).total_seconds() / 60)

        soup = BeautifulSoup(html, "html.parser")
        out: dict[frozenset, str] = {}
        for table in soup.select("table.result"):
            rows = table.select("tr")
            for idx, row in enumerate(rows):
                # A match occupies exactly two <tr>: the first carries the time in
                # a rowspan=2 cell, the second carries only the opponent. So anchor
                # on the time cell and read the opponent from the NEXT row.
                #
                # An earlier version instead walked every name in document order and
                # paired them with an alternating toggle. That carried state ACROSS
                # matches, so a single row without a name link desynced the toggle
                # and every following match in that table was paired opponent-to-
                # next-player -- with the previous match's clock. It stamped
                # "Min Ho Ko vs Philippov" (a Kalshi market for tomorrow) with the
                # 06:12 start of Philippov vs Krivoshchekov, played today. Anchoring
                # each match to its own time cell cannot drift: nothing is carried
                # between iterations.
                cell = row.select_one("td.first.time") or row.select_one("td.time")
                if cell is None:
                    continue
                m = _re.match(r"(\d{1,2}):(\d{2})", cell.get_text(strip=True))
                nxt = rows[idx + 1] if idx + 1 < len(rows) else None
                if m is None or nxt is None:
                    continue
                # The opponent row must NOT own a time cell -- if it does, this pair
                # of rows is two different matches and the layout is not what we think.
                if nxt.select_one("td.first.time") or nxt.select_one("td.time"):
                    continue
                # tennisexplorer numbers the halves rNNN / rNNNb. Where both ids are
                # present, insist they match: a free structural check on the assumption above.
                rid, nid = row.get("id"), nxt.get("id")
                if rid and nid and nid != f"{rid}b":
                    continue
                a = [x.get_text(strip=True) for x in row.select("td.t-name a")]
                b = [x.get_text(strip=True) for x in nxt.select("td.t-name a")]
                # Doubles list both partners in one cell; they never map to a
                # singles market, so anything other than one name a side is skipped.
                if len(a) != 1 or len(b) != 1:
                    continue
                total = int(m.group(1)) * 60 + int(m.group(2)) - offset
                start = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(minutes=total)
                # Key on the PLAYER PAIR, order-independent. Keying on a single
                # surname forced a "today only" scope to avoid stamping old
                # fixtures that share a player -- and match_date is itself
                # unreliable, so Sorger vs Kopp (dated 2026-08-01 but played
                # today) was skipped entirely and kept a stale platform time
                # while already in its second set.
                out[frozenset((a[0], b[0]))] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        return out

    def get_results_day(self, year: int, month: int, day: int) -> list[dict]:
        """One request returns every tier's completed matches for this
        single calendar day (see module docstring on why `type=all` is used
        regardless of tier -- the param doesn't actually filter). Only
        Challenger/ITF rows are meaningful to the caller (see
        app/ingestion/tennis_data.py) -- tour-level rows are still parsed
        and returned here (cheap, already have the HTML) so a future caller
        could cross-check against tennis-data.co.uk if ever needed, but
        aren't currently persisted twice."""
        resp = self._client.get(
            f"{BASE_URL}/results/", params={"type": "all", "year": year, "month": month, "day": day}
        )
        match_date = f"{year:04d}-{month:02d}-{day:02d}"
        return parse_results_html(resp.text, match_date=match_date)


def parse_results_html(html: str, match_date: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    # EVERY result table, not just the first. select_one() read only table 0 and
    # silently discarded the rest -- the page carries 5 (confirmed live
    # 2026-08-03), so most of the day's matches never reached the caller. That
    # one call is why the results backlog sat at ~1,276 unfinished matches with
    # past dates, and therefore why finished/in-play matches kept their
    # winner=None and stayed eligible for recommendations: Tyler Zink was in
    # table 1, already 2 sets up, and simply never parsed.
    tables = soup.select("table.result")
    if not tables:
        return []

    matches: list[dict] = []
    for table in tables:
        # State is reset PER TABLE inside the helper -- a tournament header must
        # not carry across, and a half-built pending_row must never pair a player
        # from one table with a player from the next.
        _parse_result_table(table, match_date, matches)
    return matches


def _parse_result_table(table, match_date: str | None, matches: list[dict]) -> None:
    current_tourney: dict | None = None
    pending_row: dict | None = None

    for row in table.select("tr"):
        classes = row.get("class") or []
        if "head" in classes:
            link = row.select_one("td.t-name a")
            if not link or not link.get("href"):
                current_tourney = None
                continue
            href = link["href"].strip("/")
            parts = href.split("/")  # e.g. "nottingham-challenger/2018/atp-men"
            if len(parts) < 3:
                current_tourney = None
                continue
            slug, _season_str, suffix = parts[0], parts[1], parts[2]
            tour = _tour_from_suffix(suffix)
            if tour is None:
                current_tourney = None
                continue
            current_tourney = {
                "slug": slug,
                "tier": classify_tier(slug),
                "tour": tour,
                "tourney_name": link.get_text(strip=True),
            }
            pending_row = None
            continue

        if current_tourney is None:
            continue

        name_link = row.select_one("td.t-name a")
        result_cell = row.select_one("td.result")
        if not name_link or result_cell is None:
            continue

        score_cells = [c.get_text(strip=True) for c in row.select("td.score")]
        row_text = row.get_text(" ", strip=True)
        player = {
            "name": name_link.get_text(strip=True),
            "slug": name_link["href"].strip("/").rsplit("/", 1)[-1] if name_link.get("href") else None,
            "sets_won": result_cell.get_text(strip=True),
            "scores": score_cells,
            "is_retirement": bool(re.search(r"\bret\.?\b", row_text, re.IGNORECASE)),
        }
        odds_a = row.select_one("td.coursew")
        odds_b = row.select_one("td.course")

        if pending_row is None:
            pending_row = {
                "player": player,
                "odds_a": _parse_odds(odds_a.get_text(strip=True)) if odds_a else None,
                "odds_b": _parse_odds(odds_b.get_text(strip=True)) if odds_b else None,
                "match_detail_url": _match_detail_url(row),
            }
        else:
            matches.append(_build_match(current_tourney, pending_row, player, match_date))
            pending_row = None


_SCORE_OR_WALKOVER_RE = re.compile(
    r"^\d+(\(\d+\))?-\d+(\(\d+\))?(,\s*\d+(\(\d+\))?-\d+(\(\d+\))?)*$|^w/o$", re.IGNORECASE
)


def parse_draw_html(html: str) -> list[list[str]] | None:
    """Parses tennisexplorer's tournament draw tab (confirmed live 2026-07-19
    against a real in-progress ATP 250 draw) -- an absolutely-positioned div
    grid, one column per round (`left` offset = round index), real content
    starting at `top` > 10px (the row at top=3 is just the round's text
    label, e.g. "1. round"/"quarterfinal"). Round 0 (the leftmost, widest
    column) is the FULL original bracket in play order -- every entrant's
    name (with seed/wildcard/qualifier tags like "[1]"/"[WC]"/"[Q]" still
    attached, harmless for name matching since a real player name never
    contains a literal "[") or the literal "bye". Every later round column
    interleaves each CONFIRMED occupant's name with the score of the match
    that got them there (skipped here via `_SCORE_OR_WALKOVER_RE` -- a
    walkover is real, not a score, but conveys no useful signal either) --
    the surviving names are returned in the SAME top-to-bottom bracket order
    as round 0, so round `r`'s occupant `i` is always the eventual winner of
    round `r-1`'s pair at indices `(2i, 2i+1)`. A round with fewer real
    occupants than its expected size (draw_size >> r) means that portion of
    the bracket hasn't finished yet -- see bracket_sim_tennis.py for how the
    caller uses this to start a Monte Carlo simulation from the deepest
    FULLY-resolved round rather than guessing at partial rounds.
    Returns None if the page has no draw section (tournament not found, or a
    round-robin/non-bracket format this parser doesn't support)."""
    soup = BeautifulSoup(html, "html.parser")
    draw_div = soup.select_one("#draw")
    if draw_div is None:
        return None

    by_left: dict[int, list[tuple[int, str]]] = {}
    for cell in draw_div.select("div"):
        style = cell.get("style", "")
        left_match = re.search(r"left:\s*(\d+)px", style)
        top_match = re.search(r"top:\s*(\d+)px", style)
        if not left_match or not top_match:
            continue
        text = re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()
        by_left.setdefault(int(left_match.group(1)), []).append((int(top_match.group(1)), text))

    # Real round columns have many entries; a stray 1-entry column (e.g. the
    # page's own "H2H" sidebar widget, confirmed live at a much larger `left`
    # offset than any real round) isn't one -- filtered out rather than
    # guessed at by position.
    lefts = sorted(k for k, v in by_left.items() if len(v) > 1)
    rounds: list[list[str]] = []
    for left in lefts:
        entries = sorted(by_left[left])
        names = [text for top, text in entries if top > 10 and text and not _SCORE_OR_WALKOVER_RE.match(text)]
        rounds.append(names)
    return rounds if rounds else None


def parse_tournament_surface(html: str) -> str | None:
    """Real surface for a tournament page (confirmed live 2026-07-19, e.g.
    Bastad -> "Clay") -- a small colored swatch (`td.s-color span[title]`)
    embedded in the page's own match-listing table, same Title-Case
    convention ("Hard"/"Clay"/"Grass") as tennis-data.co.uk's Surface column,
    which the rest of this app (elo_tennis.py's surface blending) already
    keys on verbatim -- confirmed matching casing, not just assumed.

    REAL BUG this fixes (caught live 2026-07-19, extending surface coverage
    past the 4 Grand Slams): a slug/year/tour_suffix that doesn't resolve to
    a real tournament does NOT 404 -- it 200s with an unrendered template
    page whose own `<title>` is the literal placeholder text
    "Tennis Explorer: [tournament]", confirmed live for 5 of 7 real
    currently-tracked tournament names tried (only 2 -- Palermo, Bastad --
    resolved to a real page). That placeholder page STILL has a
    `td.s-color span[title]` element somewhere on it (unrelated sidebar
    content), so the old version of this function returned a confident-
    looking "Clay" for EVERY one of those 5 failed lookups -- confirmed by
    testing a deliberately-nonsensical slug and getting the exact same
    "Clay" result. Guards against feeding a bogus surface into either the
    live surface backfill or the tournament-winner futures bracket sim
    (bracket_sim_tennis.py), which has been calling this same function
    since before this bug was found."""
    if "Tennis Explorer: [tournament]" in html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    span = soup.select_one("td.s-color span")
    return span.get("title") if span and span.get("title") else None


def _parse_odds(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _match_detail_url(row) -> str | None:
    link = row.select_one("a[href*='match-detail']")
    return link["href"] if link else None


def _build_match(tourney: dict, first: dict, second_player: dict, match_date: str | None) -> dict:
    p1, p2 = first["player"], second_player
    try:
        sets_a, sets_b = int(p1["sets_won"]), int(p2["sets_won"])
    except ValueError:
        sets_a = sets_b = None
    winner = None
    if sets_a is not None and sets_b is not None and sets_a != sets_b:
        winner = "a" if sets_a > sets_b else "b"
    return {
        "source_match_id": first["match_detail_url"] or f"{tourney['slug']}:{p1['slug']}:{p2['slug']}:{match_date}",
        "match_date": match_date,
        "tourney_slug": tourney["slug"],
        "tourney_name": tourney["tourney_name"],
        "tier": tourney["tier"],
        "tour": tourney["tour"],
        "player_a_name": p1["name"],
        "player_a_slug": p1["slug"],
        "player_b_name": p2["name"],
        "player_b_slug": p2["slug"],
        "winner": winner,
        "is_retirement": p1["is_retirement"] or p2["is_retirement"],
        "score": " ".join(f"{a}-{b}" for a, b in zip(p1["scores"], p2["scores"]) if a.strip() or b.strip()),
        "odds_a": first["odds_a"],
        "odds_b": first["odds_b"],
    }
