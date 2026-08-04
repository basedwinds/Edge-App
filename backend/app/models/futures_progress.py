"""Where a futures position actually stands, in the sport's own terms.

A futures bet shows an entry price, a current price and a model number, and
none of those are checkable by eye. A record is. "55-59, needs 15 of 48 left"
says the same thing as "model 100%" but you can verify it yourself, which
matters for a tracker whose whole premise is that the model isn't trusted.

DISPLAY ONLY. Nothing here feeds pricing. A record is a tempting model feature
and it is not a validated one, so it stays on the read path where being wrong
costs a wrong label instead of a wrong stake.

FAILS OPEN, always. Every path returns None when the data isn't there, and the
caller renders nothing. A blank progress cell is the correct output for a
season that hasn't started; a broken table is not.

WHAT IS DELIBERATELY MISSING
  Soccer -- soccer_matches has result columns but every 2026 row is unplayed
    (0 of 81 have goals), so a record would read "0-0-0" for every position.
  Esports tournaments -- the event names don't join. A bet reads "VCT AMER
    Stage 2 2026: Winner" while the match rows read "VCT 2026: Americas Stage
    2", and lol_matches.event_name is empty on 233 of its rows. Fuzzy-matching
    a tournament name to decide whether a position is DEAD is exactly the kind
    of guess that produces a confidently wrong status.
"""
from __future__ import annotations

import logging
import math
import unicodedata

log = logging.getLogger("futures_progress")

# Season-long team markets, by the table their results live in. Every one of
# these tables carries away_team/home_team/away_score/home_score/season.
_TEAM_TABLES = {
    "mlb": ("mlb_games", "game_type = 'R'"),
    "wnba": ("wnba_games", None),
    "cfb": ("cfb_games", None),
    "nfl": ("nfl_games", None),
    "nba": ("nba_games", None),
}

# Markets whose progress IS the team's season record.
_SEASON_MARKETS = {
    "win_total", "division_winner", "playoff_qualifier", "best_record",
    "worst_record", "conference_champion", "conference_qualifier",
    "conference_regtop", "cfb_playoff",
}


def _strip(s: str) -> str:
    d = unicodedata.normalize("NFKD", s)
    return "".join(c for c in d if not unicodedata.combining(c))


# ---------------------------------------------------------------- team records


def _team_records(session, sport: str, teams: set[str]) -> dict[str, tuple[int, int, int]]:
    """{team: (wins, losses, games_left)} for the CURRENT season. One query."""
    cfg = _TEAM_TABLES.get(sport)
    if not cfg or not teams:
        return {}
    table, extra = cfg
    from sqlalchemy import text

    where = f"season = (SELECT MAX(season) FROM {table})"
    if extra:
        where += f" AND {extra}"
    rows = session.execute(text(
        f"SELECT away_team, home_team, away_score, home_score FROM {table} WHERE {where}"
    )).all()

    out: dict[str, list[int]] = {t: [0, 0, 0] for t in teams}
    for away, home, a_score, h_score in rows:
        for team, mine, theirs in ((away, a_score, h_score), (home, h_score, a_score)):
            rec = out.get(team)
            if rec is None:
                continue
            if mine is None or theirs is None:
                rec[2] += 1          # scheduled, not yet played
            elif mine > theirs:
                rec[0] += 1
            elif mine < theirs:
                rec[1] += 1
            # a tie counts as played but as neither -- NFL/soccer only
    return {t: (w, l, left) for t, (w, l, left) in out.items() if w or l or left}


def _record_text(rec: tuple[int, int, int], line: float | None) -> dict | None:
    wins, losses, left = rec
    if wins + losses == 0:
        return None                  # season hasn't started; a 0-0 tells nothing
    base = f"{wins}–{losses}"
    if line is None:
        return {"text": f"{base} · {left} left", "tone": "neutral"}

    # A win-total line: the only season market where "how many more" is exact.
    # `line` is Kalshi's floor_strike -- an INTEGER threshold where 70 means
    # "70 or more", NOT "over 70" (kalshi_cfb_client documents this for the
    # same ladder). ceil() so a half-line, if one ever arrives, still reads as
    # the first winning whole number.
    need = math.ceil(line) - wins
    if need <= 0:
        return {"text": f"{base} · cleared {line:g}", "tone": "good"}
    if need > left:
        return {"text": f"{base} · can't reach {line:g}", "tone": "dead"}
    return {"text": f"{base} · needs {need} of {left} left", "tone": "neutral"}


# ------------------------------------------------------------ tennis knockouts


def _player_key(full_name: str, keys: set[str]) -> str | None:
    """Map "Arthur Fils" onto the stored key "fils a." -- or onto nothing.

    Never guesses. The initial must agree and the surname must be a tail of the
    full name, and if two stored keys both qualify this returns None. That
    follows the rule flashscore_tennis_client already documents: surname-only
    matching equates Petros and Stefanos Tsitsipas, and pointing a position at
    the wrong player is worse than showing no label at all. A plain substring
    test is just as bad -- "Fils" is inside "Monfils".
    """
    parts = _strip(full_name).lower().split()
    if len(parts) < 2:
        return None
    initial = parts[0][0] + "."
    hits = []
    for k in keys:
        toks = k.split()
        inits = [t for t in toks if t.endswith(".") and len(t) <= 2]
        surname = " ".join(t for t in toks if not (t.endswith(".") and len(t) <= 2))
        if not inits or not surname or inits[0] != initial:
            continue
        if " ".join(parts).endswith(surname):
            hits.append(k)
    return hits[0] if len(hits) == 1 else None


def _short(name: str) -> str:
    parts = name.split()
    return parts[-1] if len(parts) < 2 else f"{parts[-1]} {parts[0][0]}."


def _tennis_progress(session, tourney: str, player_name: str) -> dict | None:
    """Alive or out of a draw, from results alone.

    Tennis draws are single-elimination, so no round labels are needed to know
    a position is dead: losing ANY match ends it. That matters because `round`
    is NULL on every recent row in tennis_matches -- the bracket position isn't
    stored, but the only fact worth showing is still derivable.
    """
    from sqlalchemy import text

    rows = session.execute(text(
        "SELECT match_date, player_a_key, player_b_key, player_a_name, player_b_name, "
        "       winner_key, is_retirement "
        "FROM tennis_matches WHERE tourney_name = :t ORDER BY match_date"
    ), {"t": tourney}).all()
    if not rows:
        return None

    keys = {r[1] for r in rows} | {r[2] for r in rows}
    keys.discard(None)
    me = _player_key(player_name, keys)
    if me is None:
        return None

    wins = 0
    for _date, a_key, b_key, a_name, b_name, winner, _ret in rows:
        if me not in (a_key, b_key):
            continue
        if winner is None:
            continue                 # played but unscored -- says nothing yet
        if winner == me:
            wins += 1
        else:
            opp = b_name if a_key == me else a_name
            lost_to = f" to {_short(opp)}" if opp else ""
            return {"text": f"Out — lost{lost_to}", "tone": "dead"}
    if wins == 0:
        return None                  # entered but hasn't finished a match yet
    return {"text": f"Still in · {wins}–0", "tone": "good"}


# --------------------------------------------------------------------- entry


def progress_for(session, bets) -> dict[int, dict]:
    """{placed_bet.id: {"text": str, "tone": "good"|"neutral"|"dead"}}

    Only the bets it can actually speak to appear in the result.
    """
    out: dict[int, dict] = {}
    try:
        # Season records: one query per sport, not one per position.
        wanted: dict[str, set[str]] = {}
        for b in bets:
            if b.market_type in _SEASON_MARKETS and b.sport in _TEAM_TABLES and b.team:
                wanted.setdefault(b.sport, set()).add(b.team)
        records = {sport: _team_records(session, sport, teams) for sport, teams in wanted.items()}

        for b in bets:
            if b.market_type in _SEASON_MARKETS and b.team:
                rec = records.get(b.sport, {}).get(b.team)
                if rec:
                    got = _record_text(rec, b.line if b.market_type == "win_total" else None)
                    if got:
                        out[b.id] = got
            elif b.market_type == "tournament_winner" and b.sport == "tennis" and b.team and b.label:
                got = _tennis_progress(session, b.label, b.team)
                if got:
                    out[b.id] = got
    except Exception:
        # Progress is decoration. It must never be the reason a tracker 500s.
        log.warning("futures progress failed", exc_info=True)
        return {}
    return out
