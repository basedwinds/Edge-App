"""Production racing rating service (F1 / IndyCar / NASCAR). Walk-forward
driver-Elo + constructor-Elo off the historical race caches (data/racing_*.json,
built by scripts/build_racing_cache.py with grid + constructor), producing the
CURRENT ratings plus the validated strength blend that racing_sim.py consumes.

Model + per-series constants are the ones grid-searched in
scripts/racing_engine_v2.py (2026-07-23):
  strength = driver_elo + CON_W*(constructor_elo - BASE) - GRID_PTS*(grid - 1)
  F1     grid_pts=130 con_w=0.6  (grid + car both strongly predictive; Brier
                                  0.043->0.028, winner-hit 45%->62% vs v1)
  IndyCar grid_pts=60 con_w=0.0  (more chaotic/spec; constructor doesn't help)
  NASCAR  grid_pts=90 con_w=0.5  (grid weakly predictive; winner-hit 7%->18%)

Keyed by ESPN driver_id, with a normalized-name index so the Kalshi matcher can
resolve "Max Verstappen" -> our id. Grid is a PRE-RACE input (known only after
qualifying); strength() takes grid=None (no discount) when it isn't set yet, so
a pre-qualifying race still prices off driver+constructor alone.

model_validated stays False everywhere -- proven or killed by forward CLV
(paper_logger.py); racing can't be historically backtested (thin Kalshi
retention), so CLV is the ONLY real judge.
"""
import json
import re
import unicodedata
from pathlib import Path

BASE = 1500.0
SEASON_REGRESSION = 1.0 / 3.0
K_DRIVER = 24.0
K_CON = 24.0

SERIES = ("f1", "irl", "nascar", "nascar_xfinity", "nascar_truck")
# REFIT PER SERIES 2026-08-07 -- scripts/fit_racing_params_per_series.py, a
# walk-forward over each series' own race history (Cup 157 races, Xfinity 151,
# Truck 108, IndyCar 73) mirroring production's update rules exactly.
#
# grid_pts dropped from 90 to 30 for all three NASCAR series and from 60 to 30
# for IndyCar. This began as a check on whether Cup's constants suited the newly
# added lower series; the answer was that 90 did not suit CUP either.
#
#     series    grid=0   30(new)   60      90(old)     <- Brier, lower better
#     cup       .02574   .02510   .02618  .02764
#     xfinity   .02509   .02387   .02461  .02596
#     truck     .02695   .02517   .02547  .02643
#     irl       .03486   .03186   .03199  .03334
#
# FOUR INDEPENDENT SERIES land on the same interior optimum -- both flatter (0)
# and sharper (60/90) score worse in every one. That agreement is the
# cross-validation a single 24-combination grid search could not provide.
#
# CHOSEN ON CALIBRATION, NOT ON WINNER-HIT, and the two disagree for Cup and
# Truck (Cup ranks winners better at 90: 17.6% vs 14.8%). Calibration governs
# because this model never has to pick one driver -- it bets wherever model_prob
# beats the market price, so profit depends on the PROBABILITY being right, and
# winner-hit is a ranking diagnostic. grid_pts=90 ranked better precisely
# BECAUSE it was overconfident, and an overconfident model overstates its own
# favourites, manufacturing fake edges on the drivers it likes most. That is the
# same failure that flat-staked ~$3,790 across ~200 player-stat futures on
# implausible +50-60pp edges.
#
# The change also moves toward LESS confidence, so the cost of being wrong is
# fewer and smaller edges rather than more bad bets.
#
# F1 IS DELIBERATELY UNCHANGED at 130, but NOT for the reason first given. I
# justified it as "~24 races, the gap is noise" -- F1 actually has 124 races,
# comparable to the others, so the sample argument was simply wrong. The real
# reason is the gap itself: its optimum is 120 at Brier .02806, and 150 scores
# .02820, so 130 sits between two values a shade apart. Moving it would be
# chasing a difference smaller than the grid's own resolution.
#
# F1 wanting the HIGHEST grid weight while NASCAR and IndyCar want the lowest is
# a good sign the fit tracks real physics rather than noise: F1 is the series
# where track position is hardest to overcome.
#
# con_w IS DELIBERATELY UNCHANGED everywhere. Across NASCAR the differences sit
# in the 4th-5th decimal -- three constructors cannot carry much signal -- so
# refitting it would be chasing noise.
#
# The grid is COARSE (steps of 30). The claim is that 90 was too high and 30 is
# the best of the values tested, not that 30 is optimal to the point. Worth a
# finer sweep when more races exist; not at this sample size.
PARAMS = {
    "f1": {"grid_pts": 130.0, "con_w": 0.6},
    "irl": {"grid_pts": 30.0, "con_w": 0.0},
    "nascar": {"grid_pts": 30.0, "con_w": 0.5},
    "nascar_xfinity": {"grid_pts": 30.0, "con_w": 0.5},
    "nascar_truck": {"grid_pts": 30.0, "con_w": 0.5},
}

# SEPARATE parameters for FINISHING-ORDER markets (top_n). PARAMS above is fitted
# on P(1st) and stays the authority for race_winner / pole / h2h.
#
# Two parameters cannot be shared across both questions, and F1 is the proof:
# grid_pts=130 is correct for winners (pole dominates F1) and badly wrong for
# finishing order -- every reduced value fails a win-Brier constraint while every
# value that fits winners leaves top-N miles out. Nothing was broken; the model
# was fitted on one question and then asked a different one.
#
# Fitted jointly with attrition (scripts/fit_racing_joint_holdout.py) on top-N
# calibration, and validated on races the fit never saw:
#
#   series    shipped -> fitted        hold-out calibration error
#   nascar    130/0.00 -> 10.0/0.20     0.1180 -> 0.0277
#   f1        130/0.00 -> 40.0/0.10     0.0883 -> 0.0280
#   irl        30/0.00 -> 30.0/0.30     0.0806 -> 0.0192
#   xfinity    30/0.00 -> 20.0/0.30     0.0904 -> 0.0179
#   truck      30/0.00 -> 20.0/0.20     0.0995 -> 0.0321
#
# Attrition settles at a believable 0.10-0.30 here. Fitted ALONE against
# calibration it ran to 0.55 -- more than half the field breaking every race --
# because with grid_pts pinned to a winner-fitted value it was the only knob able
# to move the tail. Freeing both is what made the pair identifiable.
#
# Track-specific versions of these were built and REJECTED on hold-out
# (scripts/fit_racing_track_aware.py): the per-track physical spread is real
# (12x in grid predictiveness, 6x in attrition) but unusable at ~3.7 races per
# track. Do not re-attempt without materially more races per track.
TOPN_PARAMS = {
    "f1": {"grid_pts": 40.0, "attrition": 0.10},
    "irl": {"grid_pts": 30.0, "attrition": 0.30},
    "nascar": {"grid_pts": 10.0, "attrition": 0.20},
    "nascar_xfinity": {"grid_pts": 20.0, "attrition": 0.30},
    "nascar_truck": {"grid_pts": 20.0, "attrition": 0.20},
}


# RESTRICTOR-PLATE RACES ARE A DIFFERENT SPORT and TOPN_PARAMS prices them as if
# they were not. Daytona / Talladega / Atlanta.
#
# GRID BARELY PREDICTS FINISH THERE. Correlation of start position with finish,
# pooled across cup/xfinity/truck 2022-26:
#
#   plate 0.172 | short 0.464 | intermediate 0.455 | superspeedway 0.414 | road 0.453
#
# The other four are indistinguishable from each other, so this is ONE BINARY,
# not five track classes. That is exactly why the earlier per-track fit was
# rejected on hold-out: the signal was real but spent at ~3.7 races per track. As
# a binary it is 45 races / 1,703 driver-races.
#
# THE AGGREGATE HID IT COMPLETELY -- shipped params scored by track group:
#
#                   n   claimed   actual      gap   calib err
#   non-plate    9552     0.269    0.269   +0.000      0.0098
#   plate        1703     0.264    0.264   -0.000      0.1019
#
# Both look perfect on the mean. Plate carried 10x the error because over- and
# under-prediction cancelled: claimed 0.056 delivered 0.140 at the bottom, and
# claimed 0.544 delivered 0.376 at the top. Classic overconfidence -- the model
# spread the field on grid, a signal largely absent at plate tracks. NEVER read a
# mean gap as evidence of calibration.
#
# FITTED, then validated as a SINGLE FIXED CONSTANT on seasons it never saw --
# not per-fold optima, which flatter the result:
#
#   season   shipped(10,0.20)   plate(5,0.40)
#   2022               0.0543          0.0500   better
#   2023               0.1022          0.0276   better
#   2024               0.0818          0.0210   better
#   2025               0.0465          0.0554   worse
#   2026               0.0780          0.0497   better
#
# 4 of 5 held-out seasons improve; pooled calibration error 0.0681 -> 0.0196. The
# defect itself is repaired, not just the aggregate: the worst decile miss falls
# from 17pp to ~2pp, and the confident deciles disappear entirely, which is the
# point -- the model no longer claims 54% when grid cannot support it.
#
# Both moves are physically motivated rather than curve-fitted: LOWER grid weight
# because grid does not predict, HIGHER attrition because pack racing wrecks
# cars. 45 races is thin, so this is per-BINARY only; do not subdivide further.
# PACK-RACING (SUPERSPEEDWAY) FINISHING-ORDER PARAMETERS.
#
# Daytona / Talladega / Atlanta are a different sport and TOPN_PARAMS priced them
# as if they were not. Grid barely predicts finish there -- corr(grid, finish)
# 0.156 against 0.453 everywhere else, pooled across cup/xfinity/truck 2022-26.
#
# THE AGGREGATE HID IT COMPLETELY. Scored by group, the shipped parameters showed
# a mean gap of +-0.000 on BOTH, while pack races carried ~10x the decile
# calibration error -- over- and under-prediction cancelling exactly (claimed
# 0.056 delivered 0.140 at the bottom, claimed 0.544 delivered 0.376 at the top).
# Never read a mean gap as evidence of calibration; bucket it.
#
# THE SELECTOR IS NOT NASCAR'S FLAG ALONE, and that distinction is the whole
# reason a first version of this was fitted and thrown away. `restrictor_plate`
# marks the RESTRICTED ENGINE PACKAGE, not pack racing. For Cup the two coincide
# (Daytona/Atlanta/Talladega). For TRUCK it also covers RICHMOND, a 0.75-mile
# short track on a tapered spacer -- and wiring that version would have priced a
# short track with pack-racing parameters. Caught by a live sanity check on a real
# event, not by the fit, which looked excellent throughout.
#
# So the selector requires the flag AND a track of at least a mile (NASCAR's own
# short-track boundary). Measured bucket membership, which is asserted in
# scripts/check_racing_pack_racing.py rather than assumed:
#
#     IN   Daytona 30, Talladega 24, Atlanta 18       OUT  Richmond 5
#
# Removing Richmond SHARPENED the separation (0.172 -> 0.156) and moved attrition
# 0.40 -> 0.30, confirming it had been distorting the earlier fit.
#
# FITTED, then validated as a SINGLE FIXED constant on seasons it never saw --
# not per-fold optima, which flatter the result -- against each series' OWN
# shipped parameters:
#
#     season   shipped   pack(5.0,0.30)
#     2022      0.1006          0.0363   better
#     2023      0.1105          0.0459   better
#     2024      0.1343          0.0430   better
#     2025      0.0842          0.0419   better
#     2026      0.1413          0.0431   better
#
# 5 of 5 held-out seasons improve; pooled calibration error 0.1057 -> 0.0162. The
# defect itself is repaired, not just the aggregate: the worst decile miss falls
# from 17pp to ~2pp and the over-confident deciles disappear, which is the point
# -- the model no longer claims 54% where grid cannot support it.
#
# Both moves are physically motivated rather than curve-fitted: LOWER grid weight
# because grid does not predict, HIGHER attrition because pack racing wrecks cars.
# 43 races is thin, so this stays a BINARY; do not subdivide further.
PACK_TOPN_PARAMS = {"grid_pts": 5.0, "attrition": 0.30}


def topn_strength(series: str, driver_id: str, constructor: str | None, grid: int | None,
                  pack: bool = False):
    """strength() but with the FINISHING-ORDER grid weight. con_w is shared --
    only the grid term and attrition were refit."""
    p = PACK_TOPN_PARAMS if pack else TOPN_PARAMS.get(series)
    if p is None:
        return strength(series, driver_id, constructor, grid)
    base = strength(series, driver_id, constructor, None)
    if base is None:
        return None
    if grid is not None:
        base -= p["grid_pts"] * (grid - 1)
    return base

_DATA_DIR = Path(__file__).resolve().parents[4] / "data"

# {series: {"drivers": {id: rating}, "constructors": {name: rating},
#           "name_to_id": {normalized_name: id}, "id_to_name": {id: name}}}
_cache: dict = {}


def _norm(name: str) -> str:
    """Fold a driver name to a match key.

    NFKD-decompose first so accented letters fold to their base letter instead of
    being DELETED by the [^a-z0-9] strip. REAL BUG (found 2026-08-02 wiring up the
    IndyCar title market): ESPN spells the championship leader "Alex Palou" with
    an accent, Kalshi spells him "Alex" plain. Without the fold those normalise to
    "lexpalou" vs "alexpalou" -- so the one driver holding ~99% of the title
    probability resolved to None and his market went unpriced. Same trap waits on
    Perez/Hulkenberg/Norris-style spellings across F1 and every other racing
    market type, which is why this is fixed in the shared normaliser rather than
    at the championship lookup."""
    decomposed = unicodedata.normalize("NFKD", name or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


def _logistic(d: float) -> float:
    return 1.0 / (1.0 + 10 ** (-d / 400.0))


def _pairwise(ids, order, ratings, k):
    delta = {d: 0.0 for d in ids}
    for i in ids:
        for j in ids:
            if i != j:
                s = 1.0 if order[i] < order[j] else 0.0
                delta[i] += s - _logistic(ratings[i] - ratings[j])
    n = len(ids)
    return {d: ratings[d] + (k / (n - 1)) * delta[d] for d in ids} if n > 1 else ratings


def _compute_series(series: str) -> dict:
    path = _DATA_DIR / f"racing_{series}.json"
    if not path.exists():
        return {"drivers": {}, "constructors": {}, "quali": {}, "starts": {},
                "current_constructor": {}, "name_to_id": {}, "id_to_name": {}}
    races = list(json.loads(path.read_text(encoding="utf-8")).values())
    races.sort(key=lambda r: (r["date"] or "", r["id"]))

    drv: dict[str, float] = {}
    con: dict[str, float] = {}
    # RACE STARTS PER DRIVER. Nothing downstream could previously tell a rating
    # built on 40 starts from one built on 1 -- the state carried ratings but no
    # counts, so a driver sitting at BASE was indistinguishable from a genuinely
    # average one. Found 2026-08-14: of 263 rated Truck drivers, 215 sit within
    # 25 points of BASE and 33 are within 1 point of it, and 4 of the 7 staked
    # bets on the Richmond race were on drivers at 1499.9 against a BASE of 1500.
    #
    # DELIBERATELY NOT A GATE, and that is the point. The equivalent CS2
    # threshold was challenged on exactly this hunch and UPHELD by measurement --
    # thin ratings turned out to be the BEST-calibrated bucket there (3 games:
    # claimed .799, delivered .784; 50+: .843 vs .755), because a near-default
    # rating rarely lets the model get confident. Racing may or may not behave
    # the same: here the GRID term can carry a confident prediction on its own,
    # regardless of how thin the driver rating is, which is a real mechanistic
    # difference. This exposes the count so that question can be MEASURED rather
    # than assumed; see scripts/check_racing_start_counts.py.
    starts: dict[str, int] = {}
    quali: dict[str, float] = {}  # qualifying/pole Elo (trained on start_order)
    current_constructor: dict[str, str] = {}  # driver_id -> most recent constructor
    name_to_id: dict[str, str] = {}
    id_to_name: dict[str, str] = {}
    cur_season = None
    for race in races:
        if race["season"] != cur_season:
            cur_season = race["season"]
            for d in drv:
                drv[d] = BASE + (1 - SEASON_REGRESSION) * (drv[d] - BASE)
            for c in con:
                con[c] = BASE + (1 - SEASON_REGRESSION) * (con[c] - BASE)
            for d in quali:
                quali[d] = BASE + (1 - SEASON_REGRESSION) * (quali[d] - BASE)
        results = race["results"]
        field = [r["driver_id"] for r in results]
        for r in results:
            name_to_id[_norm(r["driver"])] = r["driver_id"]
            id_to_name[r["driver_id"]] = r["driver"]
            if r.get("constructor"):
                current_constructor[r["driver_id"]] = r["constructor"]  # chronological -> last wins
        for d in field:
            starts[d] = starts.get(d, 0) + 1
        d_rat = {d: drv.get(d, BASE) for d in field}
        order = {r["driver_id"]: r["order"] for r in results}
        drv.update(_pairwise(field, order, d_rat, K_DRIVER))
        # constructor = best finisher per constructor this race
        best = {}
        for r in results:
            c = r.get("constructor")
            if c and (c not in best or r["order"] < best[c]):
                best[c] = r["order"]
        if len(best) > 1:
            c_rat = {c: con.get(c, BASE) for c in best}
            con.update(_pairwise(list(best), best, c_rat, K_CON))
        # qualifying Elo: trained on start_order (the grid = the quali result)
        if all(r.get("start_order") for r in results):
            q_rat = {d: quali.get(d, BASE) for d in field}
            q_order = {r["driver_id"]: r["start_order"] for r in results}
            quali.update(_pairwise(field, q_order, q_rat, K_DRIVER))
    return {"drivers": drv, "constructors": con, "quali": quali, "starts": starts,
            "current_constructor": current_constructor,
            "name_to_id": name_to_id, "id_to_name": id_to_name}


def refresh_ratings():
    for series in SERIES:
        _cache[series] = _compute_series(series)


def _series_state(series: str) -> dict:
    if series not in _cache:
        _cache[series] = _compute_series(series)
    return _cache[series]


# Normalised-name aliases, applied before the lookup. Platforms spell the same
# driver differently in ways _norm cannot reconcile because the letters actually
# differ -- a given name vs the racing name ("Patricio" vs "Pato" O'Ward), or a
# dropped middle name ("Sting Robb" vs "Sting Ray Robb"). Both measured live on
# Polymarket's IndyCar race markets 2026-08-06, where they were the only two real
# drivers out of 39 entrants that failed to resolve.
#
# Keyed and valued on the NORMALISED form, so this is checked after _norm and
# before name_to_id. Kept tiny and explicit on purpose: a fuzzy surname fallback
# already exists in resolve_driver_loose, and anything looser than an exact
# alias risks resolving one driver onto another.
_DRIVER_ALIASES = {
    "patriciooward": "patooward",
    "stingrobb": "stingrayrobb",
    # F1, measured 2026-08-21 on the Dutch GP sprint -- the first race ever
    # priced off a REAL starting grid. Both are the same "extra name part" shape
    # as stingrobb: Kalshi carries the full given name / a suffix, ESPN does not.
    # These two were the ONLY names in a 22-car field that failed to resolve, and
    # the cost was not cosmetic: the field sim normalises to a fixed total (5.000
    # for a top-5 market), so an unresolved driver's mass is redistributed over
    # whoever is left. Antonelli starts P5 and the market prices him at 40.5% to
    # finish top 5; dropping him inflated George Russell to 83.4% against a 57.0%
    # market -- a +26pp "edge" at a 1.46x ratio, far too plausible-looking for
    # implausible_disagreement to catch, and it was staked at $10.
    # Each target verified unique and CURRENT in the f1 pool before adding.
    "andreakimiantonelli": "kimiantonelli",
    "carlossainzjr": "carlossainz",
    # NASCAR Cup, measured 2026-08-22 on the New Hampshire weekend -- the same
    # failure the F1 pair above caused, found by the incomplete-field gate
    # firing on a real race rather than by anyone looking. All three were
    # unresolvable on the exchange's spelling, and two Cup events (36 rows each)
    # were suppressed from staking as a result, including a +23.1pp edge.
    #   "John H. Nemechek"    vs ESPN "John Hunter Nemechek"  (initialised name)
    #   "Ricky Stenhouse"     vs ESPN "Ricky Stenhouse Jr."   (dropped suffix)
    #   "Darrell Wallace Jr"  vs ESPN "Bubba Wallace"         (racing name, the
    #                                    same shape as patriciooward above)
    # Each target verified UNIQUE and CURRENT in the nascar pool before adding.
    "johnhnemechek": "johnhunternemechek",
    "rickystenhouse": "rickystenhousejr",
    "darrellwallacejr": "bubbawallace",
    # NASCAR Truck, 2026-08-22. Same two shapes yet again -- a shortened given
    # name and a dropped suffix. Both targets verified unique and CURRENT in the
    # nascar_truck pool, with real history behind them (Ruggiero 36 starts,
    # Christopher 3), so these are drivers we CAN rate and were simply failing to
    # match. That is now five separate name failures across three series in two
    # days; see project_racing_field_completeness for the standing check this
    # argues for.
    "gioruggiero": "giovanniruggiero",
    "michaelchristopher": "michaelchristopherjr",
    # Found 2026-08-23 by auditing every racing driver name EVER ingested (197
    # distinct) rather than by a race surfacing them: 12 resolved in no pool, and
    # these two were plain name variants hiding among them. Both targets verified
    # unique and CURRENT before adding.
    "nicholassanchez": "nicksanchez",      # nascar_xfinity + nascar_truck
    "justinscarroll": "justincarroll",     # nascar_truck
}


def resolve_driver_id(series: str, name: str) -> str | None:
    """Platform driver name -> our ESPN driver_id (normalized match + aliases)."""
    key = _norm(name)
    key = _DRIVER_ALIASES.get(key, key)
    return _series_state(series)["name_to_id"].get(key)


def resolve_driver_loose(series: str, name: str) -> str | None:
    """Like resolve_driver_id but also matches a SURNAME-only label (Polymarket's
    head-to-head markets use "Colapinto vs Gasly"). Surname matching is scoped to
    drivers active THIS season (current_constructor keys) so historical namesakes
    (Jos vs Max Verstappen, Michael vs Mick Schumacher) don't collide; an
    ambiguous or unknown surname returns None (left unpriced, never guessed)."""
    exact = resolve_driver_id(series, name)
    if exact:
        return exact
    st = _series_state(series)
    id_to_name = st.get("id_to_name", {})
    target = _norm(name)
    matches = [
        i for i in st.get("current_constructor", {})
        if id_to_name.get(i) and _norm(id_to_name[i].split()[-1]) == target
    ]
    return matches[0] if len(matches) == 1 else None


def split_h2h_label(label: str) -> "tuple[str, str] | None":
    """('Ryan Blaney', 'Todd Gilliland') from a head-to-head label, else None.

    ONE implementation because there were about to be two. The pricer
    (racing_markets._h2h_model_prob) and the settlement grader
    (bet_settlement._grade_racing_h2h) each split this label independently, and
    both only knew " vs ". Adding Kalshi's wording to one and not the other
    would have priced a market nothing could grade -- the same silent shape as
    the Xfinity settlement gap found earlier today.

    TWO WORDINGS, both real. Polymarket writes "A vs B"; Kalshi writes
    "A beats B" in yes_sub_title (verified against 60 settled KXNASCARH2H
    markets, e.g. "Todd Gilliland beats Ryan Blaney"). Order is meaningful in
    both: the FIRST name is the side the bet backs.
    """
    import re

    parts = re.split(r"\s+(?:vs\.?|beats)\s+", label or "", flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(), parts[1].strip()
    return (a, b) if a and b else None


def strength(series: str, driver_id: str, constructor: str | None, grid: int | None) -> float | None:
    """Validated blend for the sim. grid=None -> no grid term (pre-qualifying).
    None if the driver has no rating (unknown entrant -> left unpriced)."""
    st = _series_state(series)
    if driver_id not in st["drivers"]:
        return None
    p = PARAMS[series]
    s = st["drivers"][driver_id]
    if constructor is not None:
        s += p["con_w"] * (st["constructors"].get(constructor, BASE) - BASE)
    if grid is not None:
        s -= p["grid_pts"] * (grid - 1)
    return s


def quali_strength(series: str, driver_id: str) -> float | None:
    """Qualifying/pole Elo for a driver (predicts the grid), None if unrated.
    Constructor is deliberately NOT added -- validated best con_w=0 for F1
    qualifying (a driver's own quali Elo already reflects the car)."""
    st = _series_state(series)
    if driver_id not in st.get("quali", {}):
        # fall back to race rating if quali not populated (e.g. a driver only in
        # races with no grid data) so a rated driver is still priceable.
        return st["drivers"].get(driver_id)
    return st["quali"][driver_id]
