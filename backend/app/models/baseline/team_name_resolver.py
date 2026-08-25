"""Resolves a market's spelling of an esports team onto the spelling that owns
its match history.

THE PROBLEM. Elo lookups are exact-string, and the market feed and the match
feed do not agree on how a team is written. So a market's spelling can carry a
rating built from almost no games while an equivalent spelling holds the real
history -- measured on Valorant: "Fokus" 0 games vs "FOKUS" 106, "Stallions" 0 vs
"Stallions Esports" 21, "Leviatan" 0 vs "LEVIATAN" 96, "Gen.G Esports" 3 vs
"Gen.G" 100. On a fixed team set, 32 of 127 active market teams could not form a
rated pair with ANYONE for this reason alone.

WHAT THIS DOES NOT DO: merge histories. Ratings are still trained from raw names
exactly as before; only the READ resolves. Three guards keep that safe:

  * a name that already has MIN_GAMES of its own history is never redirected, so
    no established team can be repointed at another team's rating;
  * the target must itself clear MIN_GAMES;
  * a key claimed by two or more viable targets resolves to NOTHING. Ambiguity is
    dropped rather than guessed -- the discipline lol_team_aliases.py already
    documents, for the same reason: an unresolved name costs one unpriced
    market, a wrong one prices a bet off another team's strength.

A BLANKET NORMALISE-AND-MERGE WAS TRIED AND REJECTED ON THE DATA. Two things
break it. ASCII-folding collapses names with no Latin characters (15 Korean,
Thai and Chinese teams, plus "^-^") to the EMPTY STRING, which would merge
fifteen unrelated teams into one -- hence the empty-key guard. And suffix
stripping alone pairs "BE esports" with "Be Jung-sang", and "EDward Gaming"
(119 games) with "Edward Esports" (2). A "a team cannot play itself" check was
tried as a safety net and rejected NONE of them: two different teams that never
happened to meet still pass. The game-count asymmetry is what actually makes the
redirect safe.

Keyed by NAME KEY rather than by observed source spelling, deliberately. An
earlier version mapped source->target across names seen in MATCHES and silently
did nothing at all, because the spellings that need resolving are the ones the
MARKETS use and those often appear in no match.
"""
import re
import unicodedata

# One trailing corporate token is dropped. Order matters only in that the
# longest match is tried first (see _name_key).
#
# "team" is here for the same reason it is stripped as a LEADING token below --
# it is decoration on either end, and the market and the scraper disagree about
# which end. Measured across all three titles' active market teams before
# shipping: exactly TWO teams gained a rating (CS2 "9z" -> "9z Team", 147 games;
# "BetBoom" -> "BetBoom Team", 178 games -- both major orgs, and both sitting in
# the BLAST Porto and Esports World Cup fields), ZERO were repointed, and ZERO
# lost one. LoL and Valorant were unaffected.
#
# The cost, stated rather than hidden: two orgs whose two spellings have
# COMPARABLE history now share a key and are refused as ambiguous rather than
# each resolving to itself -- CS2 "1WIN" (25 games) vs "1win Team" (29), and
# Valorant "NEKOMA TEAM" (10) vs "Nekoma Club" (7). That is build_canonical_by_key
# working as designed: comparable claimants are a real ambiguity and the
# dominance ratio deliberately refuses them. Neither org is in a live market, and
# a spelling that has its own history still keeps its own rating either way --
# only a zero-history market spelling landing on those keys would go unresolved.
CORPORATE_SUFFIXES = ("esports club", "e sports", "esports", "gaming", "club", "team", "clan")

# ACRONYM EXPANSIONS -- the ONE transformation here that is not orthographic,
# and therefore the one that must be justified by evidence rather than a rule.
#
# WHY IT IS NEEDED. The live feed and the historical crawl are different
# sources and abbreviate differently. Every other disagreement between them is
# reachable by folding case, punctuation or a corporate token, and the code
# above already handles those. An acronym is not: "NIP" and "Ninjas in Pyjamas"
# share no character sequence, so no mechanical rule can bridge them and the
# team simply gets rated twice.
#
# Measured 2026-08-12 (scripts/find_esports_source_spelling_splits.py, which
# pairs spellings that fill the SAME date+opponent slot from DIFFERENT sources
# and have NEVER faced each other). Of 11 CS2 candidates the resolver already
# merged 8; these were the survivors, and the cost was real:
#
#     NIP           5 series, 1561   vs  Ninjas in Pyjamas     172 series, 1589
#     NAVI Junior   5 series, 1514   vs  Natus Vincere Junior   21 series, 1588
#
# ENTRIES REQUIRE FIXTURE EVIDENCE, not recognition. Both of these were
# confirmed by two independent shared fixtures apiece (NIP/NiP: both beat M80 on
# 2026-07-21 and both played paiN on 2026-07-26). Do not add an abbreviation
# here because it looks obvious -- a wrong alias pays out the wrong side, the
# same reason lol_team_aliases refuses sponsor renames.
#
# WHY EXPANDING IN THE KEY IS SAFE. This runs before the corporate-suffix strip,
# so a SUB-ROSTER keeps its own identity for free: "NIP Impact" expands to
# "ninjas in pyjamas impact", which is a different key from "ninjas in pyjamas"
# and stays a separate team -- as it must, since NIP Impact (31 matches) and
# Young Ninjas (4) are real distinct rosters. And because the merge still goes
# through build_canonical_by_key, _DOMINANCE_RATIO remains the backstop: 172-vs-5
# resolves, a 7-vs-7 coincidence would not.
#
# SCOPE NOTE: this map is SHARED by cs2/lol/valorant, so every entry must be an
# ORG-LEVEL truth rather than a per-title convenience. All of the below are the
# same organisation in every title it fields a roster in, which is why sharing is
# safe here; an alias true in only one title would need a per-title map.
#
# Valorant sweep, 2026-08-12 (same script, vlr crawl vs live feed). 11 candidates,
# the resolver already merged 6 (FNATIC/Fnatic, Barca/Barca with cedilla,
# PCIFIC/Pcific, ENVY/Team Envy, FURIA, Gen.G). The rest were real, and note the
# two that matter most rate the STUB HIGHER than the truth -- the direction that
# invents a favourite:
#
#     TEC Esports    5, 1526  vs  Titan Esports Club  123, 1401
#     JD Gaming      5, 1511  vs  JDG Esports         123, 1405
#     XLG Gaming     2, 1537  vs  Xi Lai Gaming       177, 1598
#     DRX            3, 1464  vs  KIWOOM DRX          265, 1555
#
# "drx" -> "kiwoom drx" is a SPONSOR PREFIX, not an acronym, and lol_team_aliases
# rightly refuses sponsor renames as a general rule. It is admissible here only
# because the leading-token mechanism makes it safe in the one way that matters:
# "DRX Challengers" expands to "kiwoom drx challengers" and stays a separate key
# from the main roster, which is exactly the collision that docstring warns about.
# It also survives a sponsor change -- if the org drops Kiwoom, both the feed and
# the crawl spelling still fold onto the same key, preserving continuity.
# VALUES ARE THE BARE ORG NAME, WITH CORPORATE TOKENS ALREADY REMOVED, because
# expansion happens BEFORE the single trailing-suffix strip below. Getting this
# wrong fails silently in the safe direction (no merge), which is how the first
# attempt here was caught: "tec" -> "titan esports club" turned "TEC Esports"
# into "titan esports club esports", which strips ONE suffix to "titan esports
# club" -- while "Titan Esports Club" strips "esports club" to "titan". Two keys,
# no merge. Write the value as whatever the canonical spelling itself reduces to.
ACRONYM_EXPANSIONS = {
    "nip": "ninjas in pyjamas",
    "navi": "natus vincere",
    "tec": "titan",
    "jdg": "jd",
    "xlg": "xi lai",
}

# PER-TITLE OVERRIDES. Everything in ACRONYM_EXPANSIONS is an org-level truth
# that holds in every title the org fields a roster in, so sharing it is right.
# "drx" is the first entry that is NOT, and it is the reason this indirection
# exists rather than a fourth entry above.
#
# Valorant needs "drx" -> "kiwoom drx": its live feed says DRX (3 series, 1464)
# while the vlr crawl says KIWOOM DRX (265 series, 1555), so without it 265
# series of history are ignored.
#
# LoL must NOT have it. LoL carries BOTH "DRX Challengers" and "Kiwoom DRX
# Challengers"; expanding the leading "drx" collapses them onto one key where
# neither dominates, so build_canonical_by_key discards the key and BOTH go
# unrated. Measured: LoL unrated 59 -> 60, "DRX Challengers" losing a rating it
# had -- the "strictly worse than the split it replaced" failure this module's
# docstring warns about. LoL's own plain "DRX" already owns 320 series under
# that exact spelling and needs no help.
#
# That asymmetry is real and permanent (it is about which spellings each FEED
# uses, not about the org), so it belongs in data, not in a comment explaining
# why a fix was skipped.
EXPANSIONS_BY_TITLE = {
    "valorant": {**ACRONYM_EXPANSIONS, "drx": "kiwoom drx"},
}


def expansions_for(title: str | None) -> dict[str, str]:
    """The expansion map for one title, defaulting to the shared org-level one."""
    return EXPANSIONS_BY_TITLE.get(title or "", ACRONYM_EXPANSIONS)


def name_key(name: str, expansions: dict[str, str] | None = None) -> str:
    """Orthographic key: accents folded, case dropped, punctuation collapsed, one
    trailing corporate token removed.

    Returns "" for a name with no ASCII content. Callers MUST treat an empty key
    as unusable -- every CJK/Thai name folds to it, and grouping on it would
    merge every one of them together.

    `expansions` selects the acronym map (see EXPANSIONS_BY_TITLE); None means
    the shared org-level one. It MUST be the same map used to build the
    canonical index -- a key built under one map and queried under another
    silently misses, which is a lookup returning None on a team we can price.
    """
    if expansions is None:
        expansions = ACRONYM_EXPANSIONS
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # A LEADING "ex" IS THE ROSTER-LEFT-THE-ORG CONVENTION. "ex-Sangal ALTERS"
    # is the five players who were Sangal ALTERS, now without the org, and the
    # history belongs to them. Stripped before the "team" strip below so
    # "ex-Team Spirit" and "Team Spirit" reach the same key.
    #
    # THE PREMISE WAS TESTED, NOT ASSUMED. Every CS2 pair holding history under
    # both spellings (2026-08-24, edge-probe/ex_pairs_audit.py) was checked for
    # the two things that would falsify it -- having faced each other, and
    # having played in the same weeks against different opponents:
    #
    #     ex-ENEIDA 9 / ENEIDA 6            org window ends before stub begins
    #     ex-Zero Tenacity 8 / 69           "
    #     ex-RUBY 13 / RUBY 35              "
    #     ex-RUSTEC 18 / RUSTEC 19          "
    #     ex-TALON 13 / TALON 12            "
    #     ex-MANA eSports 13 / MANA 7       "
    #     ex-CatEvil 4 / CatEvil 14         WINDOWS OVERLAP -- premise fails here
    #
    # No pair ever met. Eight of nine show the org's history ending where the
    # ex- roster's starts, which is the shape the convention predicts. The one
    # counterexample is why this is not shipped on the strip alone: CatEvil was
    # active Jan-Aug 2024 while ex-CatEvil played that February, so they are two
    # teams. _DOMINANCE_RATIO independently refuses it (14 is not >= 5*4), as it
    # does five of the other six -- only Zero Tenacity's 69-vs-8 clears the bar.
    # So the strip's whole live effect is three lookups, each premise-verified.
    #
    # Measured across all three titles before shipping, the same bar the "team"
    # strip was held to (edge-probe/ex_strip_measure.py): CS2 gained 2 ratings
    # ('ex-GUARA' -> 'Guara' 4 games, 'ex-Sangal ALTERS' -> 'Sangal ALTERS' 4)
    # and repointed 1 ('ex-Zero Tenacity' 8 games -> 'Zero Tenacity' 69); LoL and
    # Valorant were unaffected; ZERO names lost a rating in any title.
    #
    # THE REPOINT IS THE ONE JUDGEMENT CALL, stated rather than buried: unlike a
    # misspelling with accidental history, 'ex-Zero Tenacity' has 8 real recent
    # games of its own that the redirect sets aside for the org's larger, older
    # pool. Dominance says trust the big pool and those 69 games include these
    # players, but it is not free, and it is the first thing to revisit if
    # ex-rosters ever price badly.
    #
    # Only a STANDALONE leading token: "EXTREMUM" is one word and is untouched.
    # A genuine team whose name simply starts with "Ex" keeps its own rating
    # anyway -- resolve() never redirects a name that has its own MIN_GAMES of
    # history unless a canonical dominates it 5:1.
    t = re.sub(r"^ex\s+", "", t)
    # A LEADING "team" IS DECORATION, and dropping it is worth a real bug.
    #
    # User-reported 2026-08-11: JiJieHao was priced 65.3% to beat Spirit, one of
    # the best teams in the world. The market says "Spirit"; the scraper says
    # "Team Spirit". Those were two rating keys -- 1533.9 off THREE maps against
    # 2093.1 off 197 -- so the elite side was priced as an unrated newcomer and
    # the recommendation named the wrong winner. A 559-point Elo error.
    #
    # It is not one team: nine CS2 pairs split this way, including Liquid (5
    # maps vs Team Liquid's 211), Falcons (1 vs 205) and Vitality (1 vs 201) --
    # four of the strongest teams in the game, each mispriced whenever the
    # market used the short spelling.
    #
    # Only a LEADING token is stripped, and only the bare word "team": trailing
    # corporate tokens are already handled below, and "Team" mid-name (Team
    # Spirit Academy vs Spirit Academy) still collapses correctly because both
    # sides lose the same prefix. Verified before shipping with the self-play
    # disproof this module's docstring prescribes -- across all nine pairs,
    # neither spelling ever played the other, so none are distinct orgs.
    t = re.sub(r"^team\s+", "", t)
    # Expand a LEADING acronym (see ACRONYM_EXPANSIONS). Leading-only, so a
    # sub-roster ("NIP Impact") keeps the qualifier and stays its own key; and
    # applied after the "team" strip so "Team NAVI" folds the same way.
    head, _, rest = t.partition(" ")
    if head in expansions:
        t = (expansions[head] + " " + rest).strip()
    for suffix in sorted(CORPORATE_SUFFIXES, key=len, reverse=True):
        if t.endswith(" " + suffix):
            return t[: -len(suffix) - 1].strip()
    return t


# How much more history the canonical spelling needs before it may claim a name
# that has real appearances of its own. 197-vs-3 must resolve; 7-vs-7 must not.
_DOMINANCE_RATIO = 5


def build_canonical_by_key(match_counts: dict[str, int], min_games: int,
                           expansions: dict[str, str] | None = None) -> dict[str, str]:
    """{orthographic key: the one spelling under it that owns the match history}.

    A key is usable only when EXACTLY ONE spelling clears `min_games`. Zero
    targets, or two plausible ones, resolve to nothing.

    `expansions` must match what resolve() is later called with -- see name_key.
    """
    by_key: dict[str, list[str]] = {}
    for name in match_counts:
        key = name_key(name, expansions)
        if key:
            by_key.setdefault(key, []).append(name)
    out: dict[str, str] = {}
    for key, names in by_key.items():
        strong = [n for n in names if match_counts.get(n, 0) >= min_games]
        if len(strong) == 1:
            out[key] = strong[0]
        elif len(strong) > 1:
            # TWO SPELLINGS BOTH CLEARING min_games USED TO MEAN "REFUSE", and
            # that silently dropped the worst real case: "Spirit" had exactly 3
            # settled maps and "Team Spirit" 197, so the key was thrown away and
            # an elite team kept pricing off a 3-map stub.
            #
            # Refusing is still right when the two are COMPARABLE -- that is a
            # genuine ambiguity and merging could pick the wrong org. It is not
            # right when one spelling owns virtually all the history. So the key
            # resolves only when the leader DOMINATES the runner-up.
            ranked = sorted(strong, key=lambda n: -match_counts.get(n, 0))
            top, second = match_counts.get(ranked[0], 0), match_counts.get(ranked[1], 0)
            if second > 0 and top >= _DOMINANCE_RATIO * second:
                out[key] = ranked[0]

    # ALSO INDEX A SPACE-FREE KEY. name_key collapses punctuation INTO spaces
    # but keeps them, so "The Mongolz" and "TheMongolz" land on different keys
    # and never resolve to each other. That was survivable while both spellings
    # carried their own rating; once team_name_folding merges the pool onto one
    # spelling, a lookup on the other returns None -- an UNRATED team on a
    # market we can price, which is strictly worse than the split it replaced.
    # Confirmed live 2026-08-10: 'TheMongolz' and 'Nongshim Red Force' both went
    # to None after the merge, and "Nongshim Red Force" is the spacing an
    # exchange is likely to list.
    #
    # Added as a SECOND index rather than by changing name_key, because the
    # corporate-suffix strip matches on " " + suffix and would silently stop
    # working if spaces were removed there. Only registered where it is
    # unambiguous -- if two different canonicals collapse to the same
    # space-free key, neither is indexed.
    spaceless: dict[str, set[str]] = {}
    for key, canonical in out.items():
        spaceless.setdefault(key.replace(" ", ""), set()).add(canonical)
    for key, canonicals in spaceless.items():
        if key and key not in out and len(canonicals) == 1:
            out[key] = next(iter(canonicals))
    return out


def resolve(team: str, match_counts: dict[str, int], canonical_by_key: dict[str, str],
            min_games: int, expansions: dict[str, str] | None = None) -> str:
    """The spelling that owns this team's history, or the input unchanged.

    `expansions` must match what built `canonical_by_key` -- see name_key.
    """
    if not team:
        return team
    key = name_key(team, expansions)
    own = match_counts.get(team, 0)
    if own >= min_games:
        # Having its own history is normally reason enough never to redirect --
        # but a DOMINATED spelling is the exception this missed. "Spirit" cleared
        # min_games with 3 maps while "Team Spirit" held 197, so the early return
        # fired and the redirect never happened. Only a canonical that dominates
        # by the same ratio may override, so a team with real history is never
        # pulled onto a comparable neighbour.
        canonical = canonical_by_key.get(key) or canonical_by_key.get(key.replace(" ", ""))
        if (canonical and canonical != team
                and match_counts.get(canonical, 0) >= _DOMINANCE_RATIO * own):
            return canonical
        return team
    # Try the spaced key first, then the space-free one. BOTH halves are needed:
    # build_canonical_by_key indexes the space-free key, but the QUERY still
    # folds to a spaced key, so "Nongshim Red Force" would look up
    # "nongshim red force" and miss the "nongshimredforce" entry sitting right
    # there. Caught by testing the lookup rather than the index.
    return canonical_by_key.get(key) or canonical_by_key.get(key.replace(" ", "")) or team


def count_appearances(matches, team_a_key: str = "team_a", team_b_key: str = "team_b",
                      winner_key: str = "winner") -> dict[str, int]:
    """Settled appearances per spelling. That asymmetry is what makes the
    redirect safe, so unsettled matches are deliberately not counted."""
    counts: dict[str, int] = {}
    for m in matches:
        if m.get(winner_key) is None:
            continue
        for side in (team_a_key, team_b_key):
            name = m.get(side)
            if name:
                counts[name] = counts.get(name, 0) + 1
    return counts
