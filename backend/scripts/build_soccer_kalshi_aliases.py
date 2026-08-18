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
from app.clients import espn_soccer_client  # noqa: E402
from app.ingestion import market_matcher_soccer  # noqa: E402
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
    # LEAGUES, added 2026-08-08. The same anchored join works for a league
    # fixture as for a cup tie, and the non-European leagues need it badly:
    # Kalshi uses NICKNAMES the football-data feed never uses. J-League listed
    # "Frontale" (Kawasaki Frontale), "Tokyo V" (Tokyo Verdy) and "V-Varen"
    # against football-data's "fc tokyo" / "gamba osaka" -- zero overlap, so
    # 0 of 36 markets priced. Liga MX and Argentina were partial for the same
    # reason. This is the same class as the cup gap, one league further out.
    "KXBRASILEIROGAME": "bra.1",
    "KXARGPREMDIVGAME": "arg.1",
    "KXLIGAMXGAME": "mex.1",
    "KXJLEAGUEGAME": "jpn.1",
    # Long-shipped European leagues, added 2026-08-08 after a full-catalog
    # resolution sweep found gaps nobody had looked for. These were never
    # audited this way: the leagues "worked", so their few unresolvable names
    # went unnoticed -- including Nottingham Forest and Athletic Bilbao, which
    # are not obscure. A league being live is not evidence that every club in
    # it resolves.
    #
    # Athletic Bilbao is also the reason this runs through the builder at all
    # rather than being typed in: forced-uniqueness is NOT enough for "Bilbao",
    # which is token-compatible with BOTH "ath bilbao" (SP1) and the reserve
    # side "ath bilbao b" (SP2). Two candidates means the safe answer is to let
    # a real fixture pick, not to reason about which one Kalshi "must" mean.
    "KXEPLGAME": "eng.1",
    "KXEFLCHAMPIONSHIPGAME": "eng.2",
    "KXLALIGAGAME": "esp.1",
    "KXLIGAPORTUGALGAME": "por.1",
    # Rated-but-never-listed leagues, wired 2026-08-08. 61% of their fixtures
    # resolved unaided; these close the rest.
    #
    # SP2 is the one to be careful with, and it is a good argument for the
    # fixture join over any name rule: Kalshi lists "Real Sociedad B" and
    # "Celta Fortuna", which are RESERVE sides that genuinely play in Segunda.
    # Both are token-compatible with their first teams, and mapping either one
    # there would stake money on the wrong club in the wrong division. Only a
    # real fixture can tell them apart.
    "KXBELGIANPLGAME": "bel.1",
    "KXBUNDESLIGA2GAME": "ger.2",
    "KXEFLL1GAME": "eng.3",
    "KXLALIGA2GAME": "esp.2",
    "KXSUPERLIGGAME": "tur.1",
    "KXSCOTTISHPREMGAME": "sco.1",
    # New countries, 2026-08-08. Poland and Switzerland are absent on purpose:
    # ESPN publishes no Ekstraklasa feed and returns zero Swiss events, so there
    # is nothing to join against -- which is the same reason they were refused
    # for pricing at all (their bets could never settle).
    "KXALLSVENSKANGAME": "swe.1",
    "KXELITESERIENGAME": "nor.1",
    "KXDENSUPERLIGAGAME": "den.1",
    "KXCHNSLGAME": "chn.1",
    # 2026-08-18, with the seven leagues wired into kalshi_soccer_client. These
    # need the join MORE than any league before them: on first ingest only 52 of
    # 146 team-slots resolved (36%), because Kalshi lists South American and US
    # clubs by their SHORT form while the pools are keyed on ESPN's full names --
    # "Tolima" for Deportes Tolima, "Junior" for Junior FC, "Ind. Medellin",
    # "Uni Catolica", "Est. Merida", "Dep Maldonado".
    #
    # AND THIS IS WHERE THE FIXTURE JOIN EARNS ITS KEEP, not merely tidies up.
    # Three of the unresolved names are live cross-country COLLISIONS that a
    # string rule would get catastrophically wrong:
    #     ECU1 "Barcelona"    -> resolves to SP1  (Barcelona SC, not FC Barcelona)
    #     URU1 "Racing Club"  -> resolves to ARG1 (Montevideo, not Avellaneda)
    #     COL1 "Fortaleza"    -> resolves to BRA1 (Fortaleza CEIF, not Fortaleza EC)
    # None of the three mis-priced anything, because pricing keys off the
    # FIXTURE's league (get_match_distribution(match.league, ...)) so a foreign
    # pool is never consulted -- but each would be a wrong-club map if anyone
    # typed these in by hand or matched on similarity. Let a real fixture decide.
    "KXDIMAYORGAME": "col.1",
    "KXECULPGAME": "ecu.1",
    "KXURYPDGAME": "uru.1",
    "KXVENFUTVEGAME": "ven.1",
    "KXUSLGAME": "usa.usl.1",
    "KXNWSLGAME": "usa.nwsl",
    "KXSLGREECEGAME": "gre.1",
    # CONMEBOL, 2026-08-18. These slugs have NO rating pool of their own -- a
    # Libertadores tie is two clubs from two different domestic pools -- so the
    # per-league restriction added above does not apply and resolution falls back
    # to the global map, exactly as it does for the domestic cups. The
    # global-safety check at write time is what protects these: it already
    # refused "Fortaleza" (Fortaleza CEIF of Colombia vs Fortaleza EC of Brazil)
    # and "Racing Club" (Montevideo vs Avellaneda) from this very competition.
    "KXCONMEBOLLIBGAME": "conmebol.libertadores",
    "KXCONMEBOLSUDGAME": "conmebol.sudamericana",
    # SECOND TIERS + MALAYSIA, 2026-08-18. Every one of these is a RESERVE-SIDE
    # hazard, which is the SP2 caution above repeating in four new places and
    # the reason none of these were typed in by hand.
    #
    # ned.2 is the worst of them: the Eerste Divisie fields Jong Ajax, Jong PSV,
    # Jong AZ and Jong Utrecht -- genuine reserve teams in a real division, each
    # token-compatible with a first team playing in N1. Kalshi's first listed
    # market is literally "Jong Eindhoven vs Oss". Mapping any of those to the
    # senior club would stake money on the wrong side in the wrong division.
    #
    # arg.2 and bra.2 carry the second-tier version of the collision that
    # already bit CONMEBOL: short-form South American names against full ESPN
    # ones. eng.5 adds fifth-tier clubs whose names overlap former league
    # members. The fixture join is what tells them apart; the global-safety
    # check at write time is what stops any of it leaking into another pool.
    "KXBRASILEIROBGAME": "bra.2",
    "KXARGNACBGAME": "arg.2",
    "KXENGNLGAME": "eng.5",
    "KXEERSTEDIVGAME": "ned.2",
    "KXMYSLGAME": "mys.1",
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
    # REBUILD THE POOLS WITHOUT THIS FILE'S OWN ALIASES FIRST.
    #
    # market_matcher_soccer now LOADS data/soccer_kalshi_aliases.json into
    # TEAM_ALIASES at import, so refreshing ratings here would key every pool
    # through the previous run's output -- and then the safety check below would
    # be asking whether last run's answer agrees with itself. Strip them, so the
    # pool keys are the RAW ones the data actually carries.
    for _k in list(market_matcher_soccer._FROM_FILE):
        market_matcher_soccer.TEAM_ALIASES.pop(_k, None)
    elo_service_soccer.refresh_ratings()
    states = elo_service_soccer._cache["states_by_league"]
    # GLOBAL name -> league, and a per-league membership set.
    #
    # THE setdefault BELOW IS LEAGUE-BLIND ON PURPOSE, and it is why the
    # per-league set exists. A club name that appears in two pools is claimed
    # here by whichever league dict-iteration reached first, which is arbitrary.
    # Measured 2026-08-18 on the first run including the South American leagues,
    # that produced two WRONG maps and one ambiguous one:
    #   "Liverpool M"            -> liverpool  claimed E0   (it is Liverpool MONTEVIDEO, URU1)
    #   "Academia Anzoategui FC" -> portuguesa claimed BRA1 (Portuguesa FC is VEN1)
    #   "Gijon"                  -> sp gijon   claimed SP1  (also in SP2)
    # Neither wrong one could have mis-priced a domestic row -- pricing keys off
    # the FIXTURE's league via get_match_distribution(match.league, ...), so a
    # foreign pool is never consulted -- but the MAP would have been wrong, and
    # this file is read by the cross-league resolver where the league is exactly
    # what is in question.
    #
    # So resolution is now restricted to the league the ESPN SLUG belongs to,
    # whenever that slug has a pool of its own. Cups keep the global fallback:
    # ita.coppa_italia has no pool because its clubs come from I1 and I2.
    rated: dict[str, str] = {}
    rated_in: dict[str, set] = {lg: set(st.attack_log) for lg, st in states.items()}
    for lg, st in states.items():
        for team in st.attack_log:
            rated.setdefault(team, lg)
    slug_league = {slug: lg for lg, slug in espn_soccer_client.LEAGUE_CODES.items()
                   if lg in rated_in}
    espn_alias = json.loads(ESPN_ALIASES.read_text(encoding="utf-8")) if ESPN_ALIASES.exists() else {}
    if not espn_alias:
        print("NO ESPN ALIASES -- run build_soccer_espn_aliases.py first"); sys.exit(1)

    def _ok(key: str, slug: str | None) -> bool:
        """Is this canonical key rated, within the slug's own league if it has
        one? `slug=None` keeps the old global behaviour for callers that have no
        competition in hand."""
        lg = slug_league.get(slug or "")
        if lg is not None:
            return key in rated_in[lg]
        return key in rated

    def _league_for(key: str, slug: str | None) -> str:
        """Which league to RECORD for a resolved club.
        The slug's own league when it has a pool, because that is the league the
        fixture was actually played in. Falling back to rated[key] -- the
        league-blind global map -- is what wrote "Liverpool M -> liverpool, E0"
        on the first run: resolution had already been narrowed correctly, and
        then the write step threw that away and re-derived the league from a
        dict whose iteration order decided the answer."""
        return slug_league.get(slug or "") or rated[key]

    def resolve_espn(name: str, slug: str | None = None):
        entry = espn_alias.get(name)
        if entry:
            k = canonical_team_key(entry["team"])
            if _ok(k, slug):
                return k
        k = canonical_team_key(name)
        return k if _ok(k, slug) else None

    def resolve_kalshi(name: str, slug: str | None = None):
        k = canonical_team_key(name)
        return k if _ok(k, slug) else None

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
    print(f"{len(kalshi_fx)} distinct Kalshi fixtures")

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
    print(f"{sum(len(v) for v in espn_fx.values())} ESPN fixtures in the same windows\n")

    inferred: dict[str, set] = collections.defaultdict(set)
    orphans: list[tuple] = []   # fixtures where NEITHER side resolves -- stage 2
    for slug, d, a, b in kalshi_fx.values():
        ka, kb = resolve_kalshi(a, slug), resolve_kalshi(b, slug)
        if ka is None and kb is None:
            orphans.append((slug, d, a, b))
            continue
        if (ka is None) == (kb is None):
            continue  # need exactly one resolved side to anchor on
        anchor, unknown_name = (ka, b) if ka else (kb, a)
        hits = []
        for ed, en1, en2 in espn_fx.get(slug, []):
            if abs((ed - d).days) > 1:
                continue
            r1, r2 = resolve_espn(en1, slug), resolve_espn(en2, slug)
            if r1 == anchor:
                hits.append((en2, r2))
            elif r2 == anchor:
                hits.append((en1, r1))
        if len(hits) != 1:
            continue  # ambiguous or absent -- teaches nothing
        _espn_name, target = hits[0]
        if target is None:
            continue  # opponent isn't rated either; nothing to map to
        inferred[unknown_name].add((target, _league_for(target, slug)))

    # ---- STAGE 2: PAIR BOOTSTRAP -----------------------------------------
    # Stage 1 needs one KNOWN side to anchor on. That is fine for a cup, where
    # the Serie A club is always already rated -- but it cannot start a league
    # where NO club has ever been seen. The J-League is exactly that: Kalshi
    # lists "Tokyo V vs Frontale", "V-Varen vs Kyoto Sanga", and football-data's
    # JPN.csv says "fc tokyo" / "gamba osaka". Neither side resolves, every
    # fixture is skipped, and stage 1 produced 0 of 36.
    #
    # So bootstrap the PAIR instead of one side. Token-prefix compatibility
    # proposes candidates ("Frontale" could be ESPN's "Kawasaki Frontale";
    # "Tokyo V" could be "Tokyo Verdy"), and then the FIXTURE decides: keep the
    # assignment only if exactly ONE ESPN fixture within +/-1 day pairs a
    # candidate-for-A against a candidate-for-B. The string never decides
    # anything on its own -- it only narrows the search, and a fixture that
    # admits two readings is dropped rather than guessed. That distinction is
    # the whole reason this is safe where similarity scoring was not: the
    # Rangers->Angers class of error cannot survive a real-fixture check.
    def tokens(name: str) -> list[str]:
        return [t for t in re.split(r"[^a-z0-9]+", canonical_team_key(name)) if t]

    def compatible(kalshi_name: str, espn_name: str) -> bool:
        kt, et = tokens(kalshi_name), tokens(espn_name)
        if not kt or not et:
            return False
        # every Kalshi token must prefix some ESPN token (or the reverse), so
        # "frontale" fits "kawasaki frontale" and "tokyo v" fits "tokyo verdy",
        # while "tokyo v" does NOT fit "fc tokyo".
        fwd = all(any(e.startswith(k) for e in et) for k in kt)
        rev = all(any(k.startswith(e) for k in kt) for e in et)
        return fwd or rev

    boot = 0
    for slug, d, a, b in orphans:
        hits = []
        for ed, en1, en2 in espn_fx.get(slug, []):
            if abs((ed - d).days) > 1:
                continue
            for x, y in ((en1, en2), (en2, en1)):
                if compatible(a, x) and compatible(b, y):
                    hits.append((x, y))
        # dedupe symmetric duplicates before judging uniqueness
        hits = list({(x, y) for x, y in hits})
        if len(hits) != 1:
            continue
        x, y = hits[0]
        rx, ry = resolve_espn(x, slug), resolve_espn(y, slug)
        if rx is None or ry is None:
            continue  # can't map onto a rating we don't have
        inferred[a].add((rx, _league_for(rx, slug)))
        inferred[b].add((ry, _league_for(ry, slug)))
        boot += 1
    print(f"stage 2 bootstrapped {boot} of {len(orphans)} both-sides-unknown fixtures\n")

    aliases, unresolved = {}, []
    claims: dict[str, str] = {}
    scoped_note: list = []
    for name, targets in sorted(inferred.items()):
        if len(targets) != 1:
            unresolved.append((name, f"infers to {sorted(targets)}"))
            continue
        target, league = next(iter(targets))
        # Claimed PER LEAGUE, not globally, since scoped aliases were added
        # below: two different leagues legitimately map different Kalshi names
        # onto different clubs, and a global key made the second one look like a
        # duplicate. Within one league a repeat target is still a real conflict
        # and is still refused.
        if (league, target) in claims:
            unresolved.append((name, f"{target} already claimed by "
                                     f"{claims[(league, target)]} in {league}"))
            continue
        # GLOBAL-SAFETY CHECK. TEAM_ALIASES is league-BLIND -- one dict for the
        # whole app -- but every alias here is league-SCOPED. Writing a
        # league-scoped alias into a global table is only sound when the Kalshi
        # name means nothing anywhere else.
        #
        # It does not always. On the 2026-08-18 run this rule caught
        # "Barcelona" -> barcelona sc (ECU1): applied globally it also rewrote
        # LA LIGA's Barcelona, whose raw pool key is "barcelona", merging FC
        # Barcelona and Barcelona SC of Ecuador onto ONE rating key across two
        # pools. Nothing looked broken -- within-league pricing canonicalises
        # both sides identically, so the board went from 9% to 87% priced and
        # said nothing -- but the cross-league resolver that UEFA and the cups
        # use would then have had two candidate pools for a club that plays in
        # the Champions League every year.
        key = canonical_team_key(name)
        foreign = sorted(lg for lg, members in rated_in.items()
                         if lg != league and key in members)
        if foreign:
            # NOT DROPPED ANY MORE (2026-08-18). Refusing kept the app correct
            # and left the club unpriced -- 29 refusals on the run that added
            # the second tiers, which was most of why those leagues priced at
            # 17-72%. The alias is still WRONG globally and RIGHT inside its own
            # league, so it is written with a `scope` and consulted only when
            # the caller knows the league (market_matcher_soccer.LEAGUE_ALIASES).
            #
            # The evidence is unchanged: the target was proved by a real
            # date-aligned fixture IN THAT LEAGUE. What changes is only how
            # widely it is allowed to apply.
            claims[(league, target)] = name
            aliases[name] = {"team": target, "league": league, "scope": league}
            scoped_note.append((name, target, league, foreign))
            continue
        claims[(league, target)] = name
        aliases[name] = {"team": target, "league": league}

    print(f"{'Kalshi name':28s} -> {'football-data key':26s} {'lg':4s}")
    for name, v in sorted(aliases.items()):
        print(f"{name[:28]:28s} -> {v['team'][:26]:26s} {v['league']:4s}")
    n_scoped = len(scoped_note)
    print("")
    print(f"{len(aliases)} aliases ({n_scoped} LEAGUE-SCOPED), "
          f"{len(unresolved)} unresolved")
    for nm, tg, lgc, fg in sorted(scoped_note):
        print(f"   SCOPED[{lgc:5s}] {nm[:24]:24s} -> {tg[:26]:26s} "
              f"(global would have hit {fg})")
    for name, why in unresolved:
        print(f"   UNRESOLVED {name[:30]:30s} {why}")

    OUT.write_text(json.dumps(aliases, indent=1, sort_keys=True), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
