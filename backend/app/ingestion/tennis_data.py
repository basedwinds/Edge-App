"""Tennis match data ingestion -- merges two heterogeneous free sources into
one TennisMatch-shaped stream, parallel to ufc_data.py/nfl_data.py, same
"parallel modules per sport" architecture call.

- tennis-data.co.uk (see app/clients/tennisdata_client.py): ATP/WTA
  TOUR-LEVEL only, real point-in-time WRank/WPts, real bookmaker odds
  (Bet365/Pinnacle), current through 2026-07-12 (confirmed live 2026-07-18).
- tennisexplorer.com (see app/clients/tennisexplorer_client.py): Challenger
  + ITF, scraped day-by-day and cached locally (scripts/build_tennis_match_cache.py)
  -- also has real embedded odds at these tiers, closing the gap the user's
  earlier standalone tennis-model project hit (that project only checked
  tennis-data.co.uk and Sackmann's now-404 repos, never tennisexplorer.com).

Player identity: both sources render names as "Surname I." on their
match-level pages/rows -- normalize_player_key() lowercases + strips
accents + collapses whitespace on that exact string. This is a REAL,
documented simplification (see TennisMatch's own docstring in
app/db/models.py): two different players sharing surname + first initial
within the same tour/gender collide. Not fixed here -- tennisexplorer's own
per-player URL slug (a genuinely stable id, confirmed collision-resistant
via disambiguating suffixes like "raonic-f147b") would be a real upgrade,
but building a full name-to-slug cross-reference for tennis-data.co.uk's
Surname-I.-only rows is deferred as a follow-up, not required to ship a
valid Phase 1 baseline.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import pandas as pd

from app.clients import tennisdata_client
from app.ingestion.cache_memo import memoize_on_files


def _safe_int(value) -> int | None:
    """tennis-data.co.uk uses "NR" (not ranked) as a literal string in
    WRank/LRank for some rows -- not a numeric rank, and not something to
    guess a number for."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value) -> float | None:
    """tennis-data.co.uk uses "-" as a literal placeholder for a missing
    odds quote on some rows (a bookmaker simply didn't offer that market for
    that match) -- not a real odds value."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_tennisdata_sets(row: pd.Series, winner_is_a: bool) -> list[list[int]]:
    """Real per-set game counts from tennis-data.co.uk's W1..W5/L1..L5
    columns (winner's/loser's games won in each set) -- reordered into
    player_a/player_b order (same swap as names/rank/odds above). Stops at
    the first NaN set (unplayed sets stay NaN, never zero-filled)."""
    sets: list[list[int]] = []
    for i in range(1, 6):
        w_val, l_val = row.get(f"W{i}"), row.get(f"L{i}")
        if pd.isna(w_val) or pd.isna(l_val):
            break
        w_games, l_games = _safe_int(w_val), _safe_int(l_val)
        if w_games is None or l_games is None:
            break
        sets.append([w_games, l_games] if winner_is_a else [l_games, w_games])
    return sets


def _parse_set_game_count(raw: str) -> int:
    """Real per-set game counts fall into two genuinely different shapes on
    tennisexplorer, and the two are only distinguishable by the leading
    digit:
      - A tiebreak-set LOSER's raw text has the breaker's point count glued
        on with no separator (e.g. "67" = "6 games, lost the breaker 7-x").
        This ALWAYS starts with "6" -- you can only reach a breaker at 6-6,
        so the loser's own game count is always exactly 6, never anything
        else, confirmed live across real examples ("67-7", "7-66", "64-7").
        A genuine advantage-scoring set (no breaker, common at Challenger/
        ITF level) can never show a bare "6" for either side either, since
        the set only stops at 6 games if a breaker is played -- without one,
        play continues past 6-6 (7-5, 9-7, 13-11, ...). So "starts with 6
        and has extra digits" is an unambiguous signal to take just that
        leading "6", never a false positive against a real long set.
      - A genuine long advantage set (e.g. "13-11", "17-15" -- both real,
        confirmed live in this data) is a real 2-digit game count and must
        be kept as-is, not truncated -- REAL BUG in an earlier version of
        this function fixed here: blindly taking the first digit of ANY
        multi-character capture mangled these into nonsense ("13-11" ->
        "1-1"), caught by finding real examples with BOTH sides multi-digit
        that don't fit the tiebreak-suffix shape at all."""
    if len(raw) > 1 and raw[0] == "6":
        return 6
    return int(raw)


def _parse_score_string(score: str | None) -> list[list[int]]:
    """tennisexplorer's own "score" field is already player_a-first per set
    (e.g. "6-4 3-6 6-3") -- see tennisexplorer_client.py's _build_match,
    which builds this from p1 (first row) vs p2 (second row) in that exact
    order.

    REAL BUG fixed here (caught while deriving per-set game-total
    constants, not during the original build): tiebreak scores render as
    e.g. "7-62" (7-6, loser scored 2 in the breaker) -- but the tiebreak
    LOSER isn't always the second-listed player. Whichever side of the
    hyphen belongs to the set-LOSER (always the side that reached only 6,
    never 7, in a tiebreak set) gets the extra digit(s) glued on -- and
    that can be either the first or second number depending on who actually
    lost that particular set, confirmed live via real examples like
    "67-7" (a 6-7 set where the FIRST-listed player lost the breaker 7-x,
    not the second). The original version of this parser only ever
    stripped extra digits from the SECOND number, silently producing
    nonsense values like 67 games in one set for the ~7,674 real matches
    where the first-listed player happened to be the one who lost that
    set's breaker -- caught by sanity-checking the real distribution of
    parsed set-game-totals (a `67-7`-shaped bug produces obviously
    impossible ~65-76 combined-game "sets"). Fixed by checking BOTH sides
    independently via _parse_set_game_count."""
    if not score:
        return []
    sets = []
    for part in score.split():
        m = re.match(r"^(\d{1,3})-(\d{1,3})$", part)
        if not m:
            continue
        sets.append([_parse_set_game_count(m.group(1)), _parse_set_game_count(m.group(2))])
    return sets

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
TENNISDATA_CACHE_PATH = DATA_DIR / "tennisdata_matches_cache.json"
TENNISEXPLORER_CACHE_PATH = DATA_DIR / "tennisexplorer_matches_cache.json"

NO_PLAY_COMMENTS = tennisdata_client.NO_PLAY_COMMENTS


def normalize_player_key(name: str | None) -> str | None:
    """"Djokovic N." -> "djokovic n." (accent-stripped, whitespace-collapsed).
    See module docstring for the real, documented collision risk this
    implies. Returns None for missing/empty input rather than an empty
    string key (which would silently collide every unknown player together)."""
    if not name or not name.strip():
        return None
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _row_to_tennisdata_match(row: pd.Series) -> dict | None:
    winner_name, loser_name = row.get("Winner"), row.get("Loser")
    winner_key, loser_key = normalize_player_key(winner_name), normalize_player_key(loser_name)
    if winner_key is None or loser_key is None:
        return None
    date = row.get("Date")
    if pd.isna(date):
        return None
    comment = row.get("Comment")
    is_retirement = comment in NO_PLAY_COMMENTS and comment not in ("Cancelled", "Sched", "Awarded")
    played = comment not in NO_PLAY_COMMENTS or comment == "Retired"
    # tennis-data.co.uk's own "Comment" column: "Completed" (normal),
    # "Retired" (real play, real winner, excluded from Elo scoring same as
    # ufc_data.py excludes no-contests), "Walkover"/"Disqualified"/
    # "Cancelled"/"Sched"/"Awarded" (no real play at all -- winner_key kept
    # for reference but is_retirement AND a separate "no real play" case
    # both need excluding; NO_PLAY_COMMENTS covers all of these, "Retired"
    # is deliberately NOT in that set since it's the one case with real play).
    odds_w = row.get("PSW") if pd.notna(row.get("PSW")) else row.get("B365W")
    odds_l = row.get("PSL") if pd.notna(row.get("PSL")) else row.get("B365L")
    rank_w = _safe_int(row.get("WRank")) if pd.notna(row.get("WRank")) else None
    rank_l = _safe_int(row.get("LRank")) if pd.notna(row.get("LRank")) else None
    odds_w_val = _safe_float(odds_w) if pd.notna(odds_w) else None
    odds_l_val = _safe_float(odds_l) if pd.notna(odds_l) else None

    # tennis-data.co.uk's own columns are Winner/Loser-ordered (every row's
    # "Winner"/"WRank"/"PSW" etc. IS the historical winner, by definition of
    # those column names) -- confirmed live: player_a would be the actual
    # winner in 99.4% of rows (the other 0.6% are unplayed/cancelled rows
    # with no real winner) if assigned directly from these columns. Real bug
    # this fixes: a per-player-a correlate/residual check (e.g. checking
    # whether head-to-head record or rank-vs-Elo divergence predicts
    # anything beyond the Elo model itself) would silently inherit that
    # near-100% bias and produce spurious correlations -- the exact same
    # class of bug this app's MMA build already found and fixed for
    # ufcstats.com's weaker (64.2%) winner-first bias (see MmaFight's
    # docstring). Brier-score backtesting itself is NOT affected by this
    # (mean squared error against "the probability assigned to whatever
    # actually happened" is mathematically order-invariant), but assigning
    # player_a/player_b NEUTRALLY here -- by normalized key, alphabetical,
    # independent of who won -- removes the bias at the source rather than
    # requiring every future consumer to remember to symmetrize.
    player_a_is_winner = winner_key <= loser_key
    return {
        "source": "tennisdata",
        "source_match_id": f"tennisdata:{row.get('_source_year')}:{row.get('tour')}:{row.name}",
        "tour": row.get("tour"),
        "tier": "tour",
        "tourney_name": row.get("Tournament"),
        "surface": row.get("Surface") if pd.notna(row.get("Surface")) else None,
        "round": row.get("Round") if pd.notna(row.get("Round")) else None,
        "best_of": _safe_int(row.get("Best of")) if pd.notna(row.get("Best of")) else None,
        "match_date": pd.Timestamp(date).date().isoformat(),
        "player_a_key": winner_key if player_a_is_winner else loser_key,
        "player_a_name": winner_name if player_a_is_winner else loser_name,
        "player_b_key": loser_key if player_a_is_winner else winner_key,
        "player_b_name": loser_name if player_a_is_winner else winner_name,
        "winner_key": (winner_key if played else None),
        "is_retirement": 1 if (played and comment == "Retired") else 0,
        "score": None,
        "sets": _parse_tennisdata_sets(row, player_a_is_winner),
        "player_a_rank": (rank_w if player_a_is_winner else rank_l),
        "player_b_rank": (rank_l if player_a_is_winner else rank_w),
        "player_a_odds": (odds_w_val if player_a_is_winner else odds_l_val),
        "player_b_odds": (odds_l_val if player_a_is_winner else odds_w_val),
    }


def load_tennisdata_matches() -> list[dict]:
    """Reads from the local cache built by scripts/build_tennis_match_cache.py
    -- never hits tennis-data.co.uk directly at request time (same
    "cache offline, don't re-fetch on every call" convention as ufc_data.py)."""
    if not TENNISDATA_CACHE_PATH.exists():
        return []
    return json.loads(TENNISDATA_CACHE_PATH.read_text())


def build_tennisdata_cache(start_year_atp: int = 2000, start_year_wta: int = 2007) -> list[dict]:
    """Fetches tennis-data.co.uk's full history (fast: a few dozen small
    xlsx downloads, not a scrape -- no checkpointing needed, unlike the
    tennisexplorer day-by-day crawl)."""
    atp = tennisdata_client.fetch_atp_matches(start_year_atp)
    wta = tennisdata_client.fetch_wta_matches(start_year_wta)
    combined = pd.concat([atp, wta], ignore_index=True) if len(atp) and len(wta) else (atp if len(atp) else wta)
    matches = []
    for _, row in combined.iterrows():
        m = _row_to_tennisdata_match(row)
        if m is not None:
            matches.append(m)
    matches.sort(key=lambda m: m["match_date"])
    TENNISDATA_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TENNISDATA_CACHE_PATH.write_text(json.dumps(matches))
    return matches


def load_tennisexplorer_matches() -> list[dict]:
    """Reads the day-by-day scraped cache built by
    scripts/build_tennis_match_cache.py. Only Challenger/ITF rows are kept
    here -- tour-level rows tennisexplorer also returns on the same page are
    dropped in favor of tennis-data.co.uk's cleaner, rank-enriched version of
    the same real matches (see module docstring).

    REAL BUG fixed here (caught while deriving game-line constants, not
    during the original build): tennisexplorer.com's own results table
    lists the WINNER'S row first (same convention as ufcstats.com's fight
    pages, which this app's MMA build already knew to guard against) --
    confirmed live on the raw crawl cache: the "winner" field is "a" in
    99.93% of rows, not ~50%, because tennisexplorer_client.py's `_build_match`
    just takes whichever row it saw FIRST as "player_a" with no
    reassignment. This is the SAME class of bug as tennis-data.co.uk's
    Winner/Loser-column bias (fixed in _row_to_tennisdata_match above) --
    it should have been checked proactively here given that exact
    precedent, not assumed neutral. Fixed the same way: reassign
    player_a/player_b by normalized-key alphabetical order, independent of
    who actually won."""
    if not TENNISEXPLORER_CACHE_PATH.exists():
        return []
    raw = json.loads(TENNISEXPLORER_CACHE_PATH.read_text())
    matches = []
    for m in raw:
        if m["tier"] not in ("challenger", "itf"):
            continue
        first_key = normalize_player_key(m["player_a_name"])
        second_key = normalize_player_key(m["player_b_name"])
        if first_key is None or second_key is None:
            continue
        winner_side = m.get("winner")  # "a" | "b" | None, in the RAW (page-order) sense
        winner_key = None
        if winner_side == "a":
            winner_key = first_key
        elif winner_side == "b":
            winner_key = second_key

        first_is_a = first_key <= second_key
        matches.append({
            "source": "tennisexplorer",
            "source_match_id": m["source_match_id"],
            "tour": m["tour"],
            "tier": m["tier"],
            "tourney_name": m["tourney_name"],
            "surface": None,  # see tennisexplorer_client.py docstring -- not exposed on this page
            "round": None,
            "best_of": 3,  # Challenger/ITF is always best-of-3 (only ATP Grand Slams are best-of-5, and those are tour-level, sourced from tennisdata instead)
            "match_date": m["match_date"],
            "player_a_key": first_key if first_is_a else second_key,
            "player_a_name": m["player_a_name"] if first_is_a else m["player_b_name"],
            "player_b_key": second_key if first_is_a else first_key,
            "player_b_name": m["player_b_name"] if first_is_a else m["player_a_name"],
            "winner_key": None if m.get("is_retirement") else winner_key,
            "is_retirement": 1 if m.get("is_retirement") else 0,
            "score": m.get("score"),
            "sets": (_parse_score_string(m.get("score")) if first_is_a
                     else [[b, a] for a, b in _parse_score_string(m.get("score"))]),
            "player_a_rank": None,
            "player_b_rank": None,
            "player_a_odds": (m.get("odds_a") if first_is_a else m.get("odds_b")),
            "player_b_odds": (m.get("odds_b") if first_is_a else m.get("odds_a")),
        })
    return matches


@memoize_on_files(lambda: [TENNISDATA_CACHE_PATH, TENNISEXPLORER_CACHE_PATH])
def load_matches() -> list[dict]:
    """Full merged, chronologically-sorted stream -- what elo_service_tennis.py
    trains the walk-forward Elo on.

    MEMOIZED, AND THE RESULT IS SHARED -- TREAT IT AS READ-ONLY, same contract
    as soccer_data.load_matches (see cache_memo). Measured 2026-08-12: 9.6s and
    502,211 matches off 280 MB of JSON, every single call. Retirement rows are KEPT (not dropped)
    since scoring code needs to see & explicitly skip them the same way
    ufc_data.py keeps no-contest rows -- silently dropping them here would
    make it impossible for a future consumer to distinguish "excluded from
    training" from "never existed"."""
    matches = load_tennisdata_matches() + load_tennisexplorer_matches()
    matches.sort(key=lambda m: (m["match_date"], m["source"], m["source_match_id"]))
    return matches
