"""One real esports fixture, stored twice, so its result never reaches the bets.

REAL BUG this fixes (found 2026-08-04 chasing "why won't LoL bets settle").
Kalshi and Polymarket spell the same team differently -- "OKSavingsBank BRION"
against "HANJIN BRION", "Kiwoom DRX" against "DRX", "NIP" against "Ninjas in
Pyjamas" -- and each platform's ingestion creates its own match row. The app then
holds ONE real fixture as TWO rows, and the result lands on whichever row the
results scraper matched while the BETS sit on the other:

    [121] DN SOOPers vs HANJIN BRION          winner=team_a   0 bets
    [165] DN SOOPers vs OKSavingsBank BRION   winner=None     6 bets

The result was never missing. It was on the twin. 8 such pairs were stranded
across LoL and CS2 (15 duplicate pairs each).

THE DETECTOR. Two rows starting in the SAME MINUTE that share EXACTLY ONE team
must have equivalent opponents, because the shared team cannot play two matches
at once. That is derived entirely from this app's own data -- no name similarity,
no external source, no roster inference (all of which were tried first and either
guessed wrong or unblocked nothing).

WHY IT STILL NEEDS A CORROBORATION GATE. The premise leans on the start times,
and those are exactly what this app has been getting wrong. When two genuinely
different fixtures carry the same (wrong) minute and share a team, the detector
pairs them wrongly -- measured live, it proposed `BASEMENT BOYS == ENJOY` and
`1WIN == Nuclear TigeRES`, both nonsense, both seen only ONCE. So a pair is only
trusted when the two names are lexically related (one contains the other's
distinctive token) OR the same pairing turns up on two or more distinct dates.
That keeps every real alias found live -- BRION, DRX, FaZe/FaZe Clan, Team
Spirit/Spirit, BIG/Berlin International Gaming -- and drops both false ones. It
also drops `NIP == Ninjas in Pyjamas`, a real alias seen only once: a missed
settlement costs nothing, a wrong one pays out the wrong side.

TIER is never crossed: an academy squad and its parent play different matches,
so `DRX Challengers` can never take `DRX`'s result (see lol_team_aliases).

NON-DESTRUCTIVE BY DESIGN. Nothing is merged or deleted. Both rows keep their own
markets and bets; the twin's result is COPIED onto the ungraded row, oriented by
which side the shared team is on. A wrong merge would be far more expensive than
a late settlement, and these rows carry real money.
"""
from __future__ import annotations

import collections
import logging
import re

from sqlalchemy.orm import Session

from app.db.models import TennisMatch, RaceEvent
from app.ingestion.lol_team_aliases import base_key, tier_of
from app.ingestion.market_matcher_lol import normalize_team_name

log = logging.getLogger("duplicate_fixtures")

# How many distinct dates a pairing must be seen on before it is trusted without
# a lexical relation. Two is enough: the false positives measured live each
# appeared exactly once, because they need two wrong start times to coincide.
MIN_UNRELATED_SIGHTINGS = 2


def _tokens(name: str | None) -> set[str]:
    return {t for t in normalize_team_name(name or "").split() if len(t) > 2}


def _lexically_related(a: str | None, b: str | None) -> bool:
    """One name is a decorated form of the other: a shared distinctive word, or
    one normalized name contained in the other ("FaZe" in "FaZe Clan")."""
    ka, kb = base_key(a), base_key(b)
    if not ka or not kb:
        return False
    if ka in kb or kb in ka:
        return True
    return bool(_tokens(a) & _tokens(b))


def find_duplicate_pairs(session: Session, model) -> list[tuple]:
    """[(row_a, row_b, shared_team_key)] for rows that are the same fixture."""
    rows = [
        m for m in session.query(model).all()
        if m.estimated_start_time and m.team_a and m.team_b
    ]
    by_minute: dict[str, list] = collections.defaultdict(list)
    for m in rows:
        by_minute[str(m.estimated_start_time)[:16]].append(m)

    candidates: list[tuple] = []
    sightings: dict[frozenset, set] = collections.defaultdict(set)
    for minute, group in by_minute.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                ka = {base_key(a.team_a), base_key(a.team_b)}
                kb = {base_key(b.team_a), base_key(b.team_b)}
                shared = ka & kb
                if len(shared) != 1:
                    continue
                only_a, only_b = ka - shared, kb - shared
                if len(only_a) != 1 or len(only_b) != 1:
                    continue
                name_a = next(t for t in (a.team_a, a.team_b) if base_key(t) in only_a)
                name_b = next(t for t in (b.team_a, b.team_b) if base_key(t) in only_b)
                if tier_of(name_a) != tier_of(name_b):
                    continue  # academy vs parent: genuinely different teams
                key = frozenset((base_key(name_a), base_key(name_b)))
                sightings[key].add(minute[:10])
                candidates.append((a, b, next(iter(shared)), key, name_a, name_b))

    trusted = []
    for a, b, shared_key, key, name_a, name_b in candidates:
        if _lexically_related(name_a, name_b) or len(sightings[key]) >= MIN_UNRELATED_SIGHTINGS:
            trusted.append((a, b, shared_key))
    return trusted


def canonical_fixture_ids(session: Session, model) -> dict[int, int]:
    """{match_id: canonical_id} where duplicate twins share one canonical id.

    Exists because two safety controls -- `crossPlatformKey` (dedupe the same
    proposition across platforms) and `capToOneRowPerGame` (the per-match
    concentration cap) -- both key on the MATCH ID. A LoL fixture stored as a
    Kalshi row AND a Polymarket row has two ids, so both controls silently do
    nothing and the same real match can be recommended twice and STAKED twice,
    at double the intended exposure. Measured live: 9 pairs showing both halves.

    Deliberately NOT a merge. The two rows are the two PLATFORMS -- different
    prices, different liquidity -- and the divergence scanner needs both. This
    only teaches the app that they are one fixture.

    Union-find, so a chain (a~b, b~c) collapses to a single key rather than
    leaving c pointing at a different representative than a.
    """
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            # Lowest id wins, so the key is stable across restarts and does not
            # depend on which row happened to be visited first.
            hi, lo = (rx, ry) if rx > ry else (ry, rx)
            parent[hi] = lo

    for a, b, _shared in find_duplicate_pairs(session, model):
        union(a.id, b.id)
    return {mid: find(mid) for mid in parent}


def _copy_result(target, twin, shared_key: str) -> bool:
    """Put the twin's result onto `target`, oriented by the SHARED team's side.

    Orientation is the whole risk here: the two rows often list the teams in
    opposite order, so copying `winner` verbatim would hand the win to the loser.
    Anchoring on the team BOTH rows contain removes the ambiguity.
    """
    if twin.winner not in ("team_a", "team_b") or target.winner is not None:
        return False
    twin_shared_is_a = base_key(twin.team_a) == shared_key
    target_shared_is_a = base_key(target.team_a) == shared_key
    shared_won = (twin.winner == "team_a") == twin_shared_is_a

    target.winner = ("team_a" if target_shared_is_a else "team_b") if shared_won else \
                    ("team_b" if target_shared_is_a else "team_a")
    # Map score travels the same way: whichever side the shared team is on.
    a, b = twin.maps_won_a, twin.maps_won_b
    if a is not None and b is not None:
        shared_maps, other_maps = (a, b) if twin_shared_is_a else (b, a)
        target.maps_won_a, target.maps_won_b = (
            (shared_maps, other_maps) if target_shared_is_a else (other_maps, shared_maps)
        )
    return True


def apply_twin_results(session: Session, model) -> int:
    """Settle-by-proxy: copy a result from a duplicate fixture row. Returns the
    number of rows newly given a result."""
    resolved = 0
    for a, b, shared_key in find_duplicate_pairs(session, model):
        for target, twin in ((a, b), (b, a)):
            if target.winner is None and twin.winner is not None:
                if _copy_result(target, twin, shared_key):
                    resolved += 1
    if resolved:
        session.commit()
        log.info("duplicate-fixture backfill: %d %s rows resolved from their twin",
                 resolved, model.__name__)
    return resolved

def canonical_tennis_fixture_ids(session: Session) -> dict[int, int]:
    """{tennis_match_id: canonical_id} collapsing rows that are the SAME match.

    Tennis needs its own rule because its duplicates are a different shape from
    the esports ones above. There the two rows spell a team differently and
    share exactly ONE name, which is what that detector keys on. Here BOTH names
    are already identical -- the same match is simply ingested twice under
    different tour/tier prefixes:

        id=1476  live:wta:itf:Gabriella Price:Misa Malkin
        id=1535  live:atp:tour:Gabriella Price:Misa Malkin

    Same players, same date, same start, same score. It also appears as two
    spellings of one event ("ATP Montreal" vs "Canadian Open"). Measured: 203
    duplicate groups / 227 redundant rows out of 1,682 settled matches, every
    one with a DIFFERENT source_match_id, so id-based dedupe cannot see them.

    No alias inference is needed or wanted, so this asks for far more than the
    esports detector does: identical player keys AND the same start minute. Two
    distinct matches between the same two players (a rematch in another event)
    cannot start in the same minute, and a same-minute coincidence between
    DIFFERENT pairings is impossible because the pairing is part of the key.

    WHY IT MATTERS: capToOneRowPerGame and crossPlatformKey both key on the
    match id, so one real match holding two ids silently disables both -- the
    per-match concentration cap included. Measured live: 148 fixtures had both
    rows carrying markets. Nothing is merged or deleted; both rows keep their
    own markets and prices, exactly like the esports version.
    """
    rows = session.query(TennisMatch).filter(
        TennisMatch.estimated_start_time.isnot(None)
    ).all()
    groups: dict[tuple, list[int]] = {}
    for r in rows:
        if not r.player_a_key or not r.player_b_key:
            continue
        key = (frozenset((r.player_a_key, r.player_b_key)), str(r.estimated_start_time)[:16])
        groups.setdefault(key, []).append(r.id)
    out: dict[int, int] = {}
    for ids in groups.values():
        if len(ids) < 2:
            continue
        canonical = min(ids)          # stable across refreshes
        for i in ids:
            out[i] = canonical
    return out


# ---------------------------------------------------------------------------
# RACING
# ---------------------------------------------------------------------------
# Words that appear in race names without identifying the race. Stripped before
# building a name key so "NASCAR Cup Series Iowa Corn 350" and
# "NASCAR: Iowa Corn 350 Powered by Ethanol Winner" reduce to the same tokens.
_RACE_STOPWORDS = frozenset({
    "nascar", "indycar", "ntt", "f1", "formula", "cup", "series", "race", "winner",
    "pole", "position", "grand", "prix", "gp", "the", "at", "presented", "by",
    "powered", "with", "of", "and", "a", "an", "to", "finish", "in", "top", "will",
    "be", "finishes", "sponsored", "presents",
    # Generic OUTCOME words. Without these, "Will X win the F1 Drivers
    # Championship" and "Will Y win the F1 Constructors Championship" shared
    # {win, championship} -- exactly the 2-token threshold -- and the two F1
    # titles merged into one "race". Dropping them leaves the distinguishing
    # words (drivers vs constructors) to decide, which is the point.
    "win", "wins", "champion", "championship", "driver",
})


def _race_name_key(name: str) -> frozenset:
    """Identifying tokens of a race name: alphanumerics, stopwords removed.

    A SET, not a sequence, because the two platforms order and decorate the name
    differently ("Brickyard 400 presented by PPG" vs "NASCAR Brickyard 400:
    Winner"). Overlap on the distinctive tokens is what identifies the race.
    """
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return frozenset(w for w in words if w not in _RACE_STOPWORDS and len(w) > 1)


def _kalshi_race_suffix(event_ticker: str) -> "str | None":
    """"KXNASCARTOP10-IOWC3PB26" -> "IOWC3PB26".

    Kalshi files EVERY market type for a race under its own series ticker, so one
    race becomes several RaceEvent rows -- the Iowa Corn 350 has five (RACE,
    TOP3, TOP5, TOP10, TOP20). The suffix after the first "-" is the same for all
    of them and is the reliable race identity; the dates are NOT (the same
    suffix has been seen carrying both 2026-08-08 and 2026-08-22).
    """
    t = (event_ticker or "")
    if not t.startswith("KX") or "-" not in t:
        return None
    suf = t.split("-", 1)[1] or ""
    # A SEASON-LONG ticker's suffix is just the year: "KXF1-26" and
    # "KXF1CONSTRUCTORS-26" both yield "26". Grouping on that merged the F1
    # drivers' and constructors' championships into one race -- two different
    # propositions, caught while verifying this helper on real rows. A real race
    # suffix is long and not purely numeric ("IOWC3PB26", "NTTICS26"), so
    # require both. Season-long events then fall through to name matching, where
    # "drivers" vs "constructors" keeps them apart.
    if len(suf) < 4 or suf.isdigit():
        return None
    return suf


def canonical_race_event_ids(session: Session) -> dict[int, int]:
    """{race_event_id: canonical_id} collapsing rows that are the SAME race.

    WHY THIS EXISTS. Racing markets were invisible to the cross-platform
    divergence scanner even after race_event_id was added to its entity key,
    because the two platforms never SHARE a RaceEvent: each poller creates its
    own from its own identifier. Measured 2026-08-06: 0 of the racing
    race_events carried markets from both sources, while F1 (33 Kalshi + 134
    Polymarket) and NASCAR (255 + 112) both had plenty of each.

    Two joins, in order of how much they can be trusted:

    1. Kalshi-to-Kalshi on the ticker suffix. Exact, no inference.
    2. Kalshi-to-Polymarket on race-NAME token overlap within a series. Names are
       the only thing both platforms carry reliably -- start_time is not, since
       the same Kalshi suffix has been seen with dates two weeks apart.

    Requires at least two distinctive shared tokens so a stray word ("400",
    "250") cannot marry two different races. Canonical id is min(), stable
    across refreshes, matching the tennis/esports helpers above.
    """
    rows = session.query(RaceEvent).all()
    by_series: dict[str, list] = {}
    for r in rows:
        by_series.setdefault(r.series or "", []).append(r)

    out: dict[int, int] = {}
    for _series, evs in by_series.items():
        groups: list[list] = []          # each group = one real race
        # --- pass 1: Kalshi suffix ---
        by_suffix: dict[str, list] = {}
        leftovers = []
        for e in evs:
            suf = _kalshi_race_suffix(e.event_ticker or "")
            if suf:
                by_suffix.setdefault(suf, []).append(e)
            else:
                leftovers.append(e)
        groups.extend(by_suffix.values())

        # --- pass 2: attach each non-Kalshi row to a group by name overlap ---
        for e in leftovers:
            key = _race_name_key(e.name or "")
            if not key:
                groups.append([e])
                continue
            best, best_n = None, 0
            for g in groups:
                shared = max((len(key & _race_name_key(x.name or "")) for x in g), default=0)
                if shared > best_n:
                    best, best_n = g, shared
            if best is not None and best_n >= 2:
                best.append(e)
            else:
                groups.append([e])

        for g in groups:
            ids = sorted(x.id for x in g)
            if len(ids) < 2:
                continue
            canonical = min(ids)
            for i in ids:
                out[i] = canonical
    return out
