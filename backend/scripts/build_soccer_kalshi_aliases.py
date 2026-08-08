"""Build data/soccer_kalshi_aliases.json -- a VERIFIED Kalshi -> football-data
club name map for cup markets, anchored on ESPN fixtures.

WHY A THIRD MAP. build_soccer_espn_aliases.py solved ESPN <-> football-data.
This is a different gap: Kalshi <-> football-data. It only shows up in cup
markets, because cups pull in Serie B and 2. Bundesliga clubs that had never
been priced against a market before, so nobody had ever needed their Kalshi
spellings. check_cup_market_coverage.py measured the damage -- 45% of live cup
fixtures "unrateable", most of them actually rated under another name (Kalshi
"Hellas Verona" vs football-data "Verona", "Kiel" vs "Holstein Kiel", "Entella"
vs "Virtus Entella", "Stabia" vs "Juve Stabia").

WHY NOT FUZZY, AGAIN. The probe that found those also matched "1860 Munich" to
BOTH "munich 1860" (correct) and "bayern munich" (catastrophic), and "Union
Brescia" to four different clubs -- union berlin, brescia, real union,
philadelphia union. Union Brescia is a Serie C refoundation, so even the
plausible-looking "brescia" is wrong. That is the third independent
demonstration in one day that string similarity cannot do this job.

THE JOIN, one step removed from the ESPN one. Kalshi cup markets are FUTURE, so
they carry no score to join on -- but they do carry a DATE (encoded in the
ticker, e.g. KXCOPPAITALIAGAME-26AUG17PALLEC -> 2026-08-17) and a club PAIR.
ESPN publishes the same cup fixtures, under names this app can already resolve
through the ESPN alias map. So:

    take a Kalshi fixture where ONE side already resolves to football-data club C
    find ESPN fixtures for that cup within +/- 1 day that involve C
    if EXACTLY ONE exists, its other side is this fixture's opponent
    -> the Kalshi name for that opponent maps to that club

No two strings are ever compared. The anchor does the work, and the uniqueness
requirement is what makes it safe: a date where C plays twice teaches nothing
and is skipped rather than guessed.

SAFETY RULES, all enforced before writing: the anchor side must resolve; the
ESPN match must be unique on that date; the inferred club must itself resolve to
a rated team; repeated observations must agree (a Kalshi name that infers to two
different clubs is dropped, not majority-voted, because the sample per fixture is
tiny); and the map must be injective. Everything else is printed as UNRESOLVED
for human eyes and never written.
"""
from __future__ import annotations

import collections
import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.base import get_json  # noqa: E402
from app.ingestion.market_matcher_soccer import canonical_team_key  # noqa: E402
from app.models.baseline import elo_service_soccer  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
ESPN_ALIASES = DATA_DIR / "soccer_espn_aliases.json"
OUT = DATA_DIR / "soccer_kalshi_aliases.json"

KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker={s}&status=open&limit=200"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates={a}-{b}&limit=500"

# Kalshi cup series -> the ESPN slug publishing the same fixtures.
CUPS = {
    "KXCOPPAITALIAGAME": "ita.coppa_italia",
    "KXCOPPAITALIAADVANCE": "ita.coppa_italia",
    "KXDFBPOKALGAME": "ger.dfb_pokal",
    "KXDFBPOKALADVANCE": "ger.dfb_pokal",
}
PAIR = re.compile(r"^(.+?)\s+vs\.?\s+(.+?)(?:\s+Winner\?|:|$)")
TICKER_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def ticker_date(ticker: str):
    m = TICKER_DATE.search(ticker or "")
    if not m:
        return None
    yy, mon, dd = m.groups()
    if mon not in MONTHS:
        return None
    try:
        return datetime.date(2000 + int(yy), MONTHS[mon], int(dd))
    except ValueError:
        return None


def main() -> None:
    elo_service_soccer.refresh_ratings()
    states = elo_service_soccer._cache["states_by_league"]
    rated: dict[str, str] = {}
    for lg, st in states.items():
        for team in st.attack_log:
            rated.setdefault(team, lg)
    espn_alias = json.loads(ESPN_ALIASES.read_text(encoding="utf-8")) if ESPN_ALIASES.exists() else {}
    if not espn_alias:
        print("NO ESPN ALIASES -- run build_soccer_espn_aliases.py first"); sys.exit(1)

    def resolve_espn(name: str):
        entry = espn_alias.get(name)
        if entry:
            k = canonical_team_key(entry["team"])
            if k in rated:
                return k
        k = canonical_team_key(name)
        return k if k in rated else None

    def resolve_kalshi(name: str):
        k = canonical_team_key(name)
        return k if k in rated else None

    # ---- gather Kalshi fixtures ------------------------------------------
    kalshi_fx: dict[tuple, tuple] = {}
    for series, slug in CUPS.items():
        try:
            markets = get_json(KALSHI.format(s=series)).get("markets", [])
        except Exception as exc:
            print(f"{series}: FAIL {exc}"); continue
        for m in markets:
            mt = PAIR.match(m.get("title") or "")
            d = ticker_date(m.get("ticker") or "")
            if not mt or not d:
                continue
            a, b = mt.group(1).strip(), mt.group(2).strip()
            kalshi_fx[(slug, d, a, b)] = (slug, d, a, b)
    print(f"{len(kalshi_fx)} distinct Kalshi cup fixtures")

    # ---- gather ESPN fixtures for the same windows -----------------------
    espn_fx: dict[str, list] = collections.defaultdict(list)
    for slug in set(CUPS.values()):
        dates = [d for (s, d, _a, _b) in kalshi_fx if s == slug]
        if not dates:
            continue
        lo, hi = min(dates) - datetime.timedelta(days=2), max(dates) + datetime.timedelta(days=2)
        try:
            data = get_json(ESPN.format(slug=slug, a=lo.strftime("%Y%m%d"), b=hi.strftime("%Y%m%d")))
        except Exception as exc:
            print(f"{slug}: ESPN FAIL {exc}"); continue
        for ev in data.get("events", []):
            try:
                cs = ev["competitions"][0]["competitors"]
                names = [c["team"]["displayName"] for c in cs]
                d = datetime.date.fromisoformat(ev["date"][:10])
            except (KeyError, IndexError, ValueError):
                continue
            espn_fx[slug].append((d, names[0], names[1]))
    print(f"{sum(len(v) for v in espn_fx.values())} ESPN cup fixtures in the same windows\n")

    inferred: dict[str, set] = collections.defaultdict(set)
    for slug, d, a, b in kalshi_fx.values():
        ka, kb = resolve_kalshi(a), resolve_kalshi(b)
        if (ka is None) == (kb is None):
            continue  # need exactly one resolved side to anchor on
        anchor, unknown_name = (ka, b) if ka else (kb, a)
        hits = []
        for ed, en1, en2 in espn_fx.get(slug, []):
            if abs((ed - d).days) > 1:
                continue
            r1, r2 = resolve_espn(en1), resolve_espn(en2)
            if r1 == anchor:
                hits.append((en2, r2))
            elif r2 == anchor:
                hits.append((en1, r1))
        if len(hits) != 1:
            continue  # ambiguous or absent -- teaches nothing
        _espn_name, target = hits[0]
        if target is None:
            continue  # opponent isn't rated either; nothing to map to
        inferred[unknown_name].add(target)

    aliases, unresolved = {}, []
    claims: dict[str, str] = {}
    for name, targets in sorted(inferred.items()):
        if len(targets) != 1:
            unresolved.append((name, f"infers to {sorted(targets)}"))
            continue
        target = next(iter(targets))
        if target in claims:
            unresolved.append((name, f"{target} already claimed by {claims[target]}"))
            continue
        claims[target] = name
        aliases[name] = {"team": target, "league": rated[target]}

    print(f"{'Kalshi name':28s} -> {'football-data key':26s} {'lg':4s}")
    for name, v in sorted(aliases.items()):
        print(f"{name[:28]:28s} -> {v['team'][:26]:26s} {v['league']:4s}")
    print(f"\n{len(aliases)} aliases, {len(unresolved)} unresolved")
    for name, why in unresolved:
        print(f"   UNRESOLVED {name[:30]:30s} {why}")

    OUT.write_text(json.dumps(aliases, indent=1, sort_keys=True), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
