"""Derive Kalshi<->Polymarket club-name aliases from REAL listings, by aligning
the two platforms' fixtures on (division, date).

WHY THIS EXISTS -- La Liga was effectively unpriced (found 2026-08-06). Soccer
ratings are keyed on football-data.co.uk's short names ("betis", "celta",
"ath madrid"); Polymarket lists full official names ("Real Betis",
"RC Celta de Vigo", "Club Atletico de Madrid"); canonical_team_key only
lowercases, so it bridges neither. 8 of 12 SP1 fixtures with active markets had
BOTH teams reading as unrated, and three fixtures were ingested TWICE (once per
platform's spelling) because the same failure defeats match_upcoming_soccer_match.

WHY NOT JUST WRITE THE TABLE BY HAND -- because the dangerous entries are
exactly the ones that look obvious. "RCD Espanyol de Barcelona" shares the token
"barcelona" with a DIFFERENT real club; "Real Racing Club" shares "real" with
Real Madrid; "Club Atletico de Madrid" has to become "ath madrid" and not
"ath bilbao". Any of those, guessed wrong, silently prices the wrong club --
which is worse than the no-baseline state being fixed.

THE EVIDENCE USED INSTEAD: both platforms list the SAME real fixtures. On a
given division+date, pair up each Polymarket fixture with the Kalshi fixture it
must be, using only the sides that already match on a distinctive shared token,
and require that pairing to be UNIQUE. Whatever the other side is then called on
each platform is a name pair observed in real data, not asserted. That is how
"Real Racing Club" is learned to be Kalshi's "Santander" (its partner
Villarreal CF/Villarreal pins the fixture) and how "Club Atletico de Madrid"
disambiguates Kalshi's bare "Atletico" to Madrid rather than Bilbao.

Every proposal is then checked to actually resolve to a rating key that exists
for that division, and rejected if two different official names would claim the
same key. Prints a ready-to-paste TEAM_ALIASES block; it deliberately does NOT
edit market_matcher_soccer.py itself, so the entries get read before they ship.

Run:  PYTHONPATH=. ./.venv/Scripts/python.exe scripts/derive_soccer_team_aliases.py
"""
from __future__ import annotations

import collections
import sys

from app.clients import kalshi_soccer_client, polymarket_soccer_client
from app.ingestion.market_matcher_soccer import TEAM_ALIASES, canonical_team_key
from app.ingestion.soccer_data import normalize_team_name
from app.models.baseline import elo_service_soccer

# Tokens that carry no club identity -- corporate/legal forms, connectives and
# the generic sporting nouns Spanish/Italian/German/French clubs share. Used
# ONLY to decide whether two names share DISTINCTIVE evidence; never to rewrite
# a name. "real" is here because it prefixes many unrelated clubs (Real Madrid,
# Real Betis, Real Sociedad, Real Racing Club) and would otherwise pair them.
_GENERIC = {
    "fc", "cf", "cd", "ca", "rc", "rcd", "ud", "sd", "sad", "ss", "as", "ac",
    "club", "de", "del", "la", "el", "los", "las", "and", "the",
    "real", "deportivo", "atletico", "athletic", "sporting", "racing", "union",
    "calcio", "sc", "afc", "bc", "cp", "spa",
}


def _tokens(name: str) -> set[str]:
    return {t for t in (normalize_team_name(name) or "").split() if t not in _GENERIC}


def _same_side(a: str, b: str) -> bool:
    """Do these two renderings share distinctive evidence of being one club?"""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    if ta & tb:
        return True
    # Already-known alias (e.g. Kalshi "Espanyol" -> "espanol") counts as evidence.
    return canonical_team_key(a) == canonical_team_key(b)


# Corporate/legal/connective tokens ONLY. Deliberately NARROWER than _GENERIC:
# "real"/"atletico"/"athletic"/"racing"/"deportivo"/"sporting" are club-IDENTIFYING
# in Spain (Real Madrid vs Real Betis vs Real Sociedad vs Real Racing Club) and
# stripping them would fabricate matches.
_CORPORATE = {
    "fc", "cf", "cd", "ca", "rc", "rcd", "ud", "sd", "sad", "ss", "as", "ac",
    "sc", "afc", "club", "de", "del", "calcio", "spa",
}


def _rated_keys(div: str) -> set[str]:
    state = elo_service_soccer.get_rating_state(div)
    return set(getattr(state, "match_counts", {}) or {}) if state else set()


def _resolve(div: str, name: str) -> tuple[str, str] | None:
    """(rating_key, evidence_tier) for a single club name, or None.

    Tier 1  exact  -- canonical_team_key already lands on a rated key.
    Tier 2  strip  -- dropping only CORPORATE tokens lands on an existing,
            previously-verified alias or a rated key. This is what turns
            Polymarket's "Club Atletico de Madrid" into "atletico madrid",
            which the alias table has mapped to "ath madrid" since long before
            this script -- chaining through an entry that was already checked,
            not inventing a new one.
    Tier 3  subset -- the name's tokens are a strict SUPERSET of exactly one
            rated key's tokens ("real betis" contains "betis"). Requires
            uniqueness, which is what keeps "rcd espanyol de barcelona" from
            claiming "barcelona": that is a shared token, not a superset of it
            plus nothing else... and even where it would be, a second candidate
            forces a drop rather than a pick.
    """
    keys = _rated_keys(div)
    exact = canonical_team_key(name)
    if exact in keys:
        return exact, "exact"

    toks = [t for t in (normalize_team_name(name) or "").split() if t not in _CORPORATE]
    stripped = " ".join(toks)
    if stripped and stripped != exact:
        via_alias = TEAM_ALIASES.get(stripped)
        if via_alias and via_alias in keys:
            return via_alias, "strip->alias"
        if stripped in keys:
            return stripped, "strip"

    tset = set(toks)
    if tset:
        cands = {k for k in keys if set(k.split()) < tset}
        if len(cands) == 1:
            return next(iter(cands)), "subset"
    return None


# Strongest first. "subset" is last AND is never auto-accepted (see main): it is
# the tier that produced a genuinely dangerous proposal on the first run --
# "rcd espanyol de barcelona" -> "barcelona", because Espanyol's name contains
# its CITY, and that city is a different real club. Uniqueness did not save it:
# there was exactly one candidate and it was the wrong club. Cross-platform
# alignment had the right answer ("espanol") and was being overruled by a string
# heuristic, which is the wrong precedence.
_TIER_RANK = {"exact": 0, "strip->alias": 1, "strip": 2, "subset": 3}


def _fixtures(rows: list[dict]) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """{(division, date): [(home, away), ...]} -- deduped, order preserved."""
    out: dict[tuple[str, str], list[tuple[str, str]]] = collections.defaultdict(list)
    for r in rows:
        div = r.get("division")
        date = str(r.get("match_date") or r.get("estimated_start_time") or "")[:10]
        if not div or not date or not r.get("home_team") or not r.get("away_team"):
            continue
        pair = (r["home_team"], r["away_team"])
        if pair not in out[(div, date)]:
            out[(div, date)].append(pair)
    return out


def _align(kalshi_fx, poly_fx) -> list[tuple[str, str, str]]:
    """[(division, polymarket_name, kalshi_name)] for every side of every
    fixture we could pin. A Polymarket fixture is only pinned when EXACTLY ONE
    Kalshi fixture on the same division+date shares distinctive evidence --
    ambiguity is dropped, never broken by a tie-break."""
    learned: list[tuple[str, str, str]] = []
    for key, ppairs in sorted(poly_fx.items()):
        div, _date = key
        kpairs = kalshi_fx.get(key, [])
        for ph, pa in ppairs:
            hits = [
                (kh, ka) for kh, ka in kpairs
                if _same_side(ph, kh) or _same_side(pa, ka)
            ]
            if len(hits) != 1:
                continue
            kh, ka = hits[0]
            learned.append((div, ph, kh))
            learned.append((div, pa, ka))
    return learned


def main() -> int:
    print("fetching live listings from both platforms ...")
    kalshi_fx = _fixtures(kalshi_soccer_client.get_moneyline_markets())
    poly_fx = _fixtures(polymarket_soccer_client.get_moneyline_markets())
    print(f"  kalshi: {sum(len(v) for v in kalshi_fx.values())} fixtures across {len({d for d, _ in kalshi_fx})} divisions")
    print(f"  polymarket: {sum(len(v) for v in poly_fx.values())} fixtures across {len({d for d, _ in poly_fx})} divisions")

    elo_service_soccer.refresh_ratings()

    learned = _align(kalshi_fx, poly_fx)
    print(f"\npinned {len(learned)} name observations from aligned fixtures\n")

    # Collapse to proposals, keeping every observed target so a club observed
    # inconsistently shows up as a conflict rather than a coin-flip.
    # Both sides of a pinned pair are the same club, so whichever side reaches a
    # rating key supplies the target for the other. That is what rescues Kalshi's
    # bare "Atletico": on its own the string is genuinely ambiguous (Madrid or
    # Bilbao?), but it was observed in the SAME fixture as Polymarket's "Club
    # Atletico de Madrid", which resolves, so the disambiguation comes from the
    # listing data rather than from anybody's football knowledge.
    # proposals[(div, src)] = {(target, tier)} -- tier kept so precedence can be
    # applied and so a weak-evidence entry can be held back for review.
    proposals: dict[tuple[str, str], set[tuple[str, str]]] = collections.defaultdict(set)

    def offer(div: str, name: str, target: str, tier: str) -> None:
        src = normalize_team_name(name) or ""
        if not src or src == target or src in TEAM_ALIASES:
            return
        if canonical_team_key(name) == target:
            return  # already resolves; no alias needed
        proposals[(div, src)].add((target, tier))

    for div, poly_name, kalshi_name in learned:
        # Take the BEST-evidenced resolution across the pair -- the two names are
        # the same club, so the side that resolves most strongly speaks for both.
        hits = [h for h in (_resolve(div, kalshi_name), _resolve(div, poly_name)) if h]
        if not hits:
            continue
        target, tier = min(hits, key=lambda h: _TIER_RANK[h[1]])
        # Evidence from an aligned fixture, so record it as such: it is stronger
        # than any string rule and must outrank one.
        offer(div, poly_name, target, "pair" if tier == "exact" else f"pair/{tier}")
        offer(div, kalshi_name, target, "pair" if tier == "exact" else f"pair/{tier}")

    # Clubs listed by only ONE platform have no partner to learn from (Real Betis
    # is Kalshi-only right now), so they fall back to the string tiers alone.
    for fx in (kalshi_fx, poly_fx):
        for (div, _date), pairs in fx.items():
            for name in (n for p in pairs for n in p):
                if canonical_team_key(name) in _rated_keys(div):
                    continue
                hit = _resolve(div, name)
                if hit:
                    offer(div, name, hit[0], hit[1])

    def rank(tier: str) -> int:
        base = tier.split("/")[-1]
        return _TIER_RANK.get(base, 0) - (10 if tier.startswith("pair") else 0)

    accepted: list[tuple[str, str, str, str]] = []
    review: list[tuple[str, str, str, str]] = []
    rejected: list[str] = []
    for (div, src), offers in sorted(proposals.items()):
        best = min(offers, key=lambda o: rank(o[1]))
        # Only a genuine disagreement AT THE SAME strength is a conflict; a weak
        # string guess losing to aligned-fixture evidence is the system working.
        rival = {t for t, tier in offers if t != best[0] and rank(tier) == rank(best[1])}
        if rival:
            rejected.append(f"{div} {src!r}: {best[0]!r} vs {sorted(rival)} at equal evidence -- dropped")
            continue
        target, tier = best
        if elo_service_soccer.get_team_match_count(div, target) == 0:
            rejected.append(f"{div} {src!r} -> {target!r}: target has NO rating history, dropped")
            continue
        overruled = {t for t, tr in offers if t != target}
        note = tier + (f"  [OVERRULED weaker: {sorted(overruled)}]" if overruled else "")
        (review if tier == "subset" else accepted).append((div, src, target, note))

    # NOTE: many source names legitimately map to ONE key -- that is the entire
    # point of an alias table ("Atletico" and "Club Atletico de Madrid" are the
    # same club). An earlier version treated that as a collision and threw both
    # away, which is why Atletico stayed unresolved. Only flag a key claimed by
    # names that do NOT share any distinctive token, i.e. plausibly real clubs.
    claims: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for div, src, target, _n in accepted:
        claims[(div, target)].append(src)
    def _ident(s: str) -> set[str]:
        # Uses _CORPORATE, NOT _GENERIC: _GENERIC strips "atletico", which left
        # "atletico" and "club atletico de madrid" with empty token sets, so they
        # looked like unrelated clubs colliding on one key and BOTH got dropped.
        # They are the same club under two renderings, which is what an alias
        # table is for.
        return {t for t in (normalize_team_name(s) or "").split() if t not in _CORPORATE}

    for (div, target), srcs in sorted(claims.items()):
        if len(srcs) > 1 and not set.intersection(*[_ident(s) for s in srcs]):
            rejected.append(f"{div} {target!r} claimed by unrelated names {srcs} -- dropped")
            accepted = [a for a in accepted if not (a[0] == div and a[2] == target)]

    print("=" * 78)
    print("ACCEPTED (paste into market_matcher_soccer.TEAM_ALIASES):")
    print("=" * 78)
    by_div: dict[str, list[tuple[str, str, str]]] = collections.defaultdict(list)
    for div, src, target, note in accepted:
        by_div[div].append((src, target, note))
    for div in sorted(by_div):
        print(f"    # {div}, derived from aligned Kalshi/Polymarket fixtures")
        for src, target, note in sorted(by_div[div]):
            print(f'    "{src}": "{target}",  # {note}')
    if review:
        print("\n" + "=" * 78)
        print("WEAK EVIDENCE -- token-subset only, NOT auto-accepted, read before using:")
        print("=" * 78)
        for div, src, target, note in sorted(review):
            print(f'    "{src}": "{target}",  # {div} {note}')
    if rejected:
        print("\n" + "=" * 78)
        print("REJECTED (needs a human look -- NOT emitted above):")
        print("=" * 78)
        for r in rejected:
            print("  " + r)

    # Anything still unresolved on either platform, so the real remaining gap is
    # visible rather than implied by absence.
    print("\n" + "=" * 78)
    print("STILL UNRESOLVED after these aliases:")
    print("=" * 78)
    pending = {(d, s): t for d, s, t, _n in accepted}
    seen: set[tuple[str, str]] = set()
    for fx, label in ((kalshi_fx, "kalshi"), (poly_fx, "polymarket")):
        for (div, _date), pairs in sorted(fx.items()):
            for name in (n for p in pairs for n in p):
                norm = normalize_team_name(name) or ""
                key = canonical_team_key(name)
                key = pending.get((div, norm), key)
                if elo_service_soccer.get_team_match_count(div, key) == 0 and (div, norm) not in seen:
                    seen.add((div, norm))
                    print(f"  {label:11} {div} {name!r} -> {key!r}")
    if not seen:
        print("  (none -- every listed club resolves to a rated key)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
