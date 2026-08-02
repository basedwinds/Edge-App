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

SERIES = ("f1", "irl", "nascar")
PARAMS = {
    "f1": {"grid_pts": 130.0, "con_w": 0.6},
    "irl": {"grid_pts": 60.0, "con_w": 0.0},
    "nascar": {"grid_pts": 90.0, "con_w": 0.5},
}

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
        return {"drivers": {}, "constructors": {}, "quali": {}, "current_constructor": {}, "name_to_id": {}, "id_to_name": {}}
    races = list(json.loads(path.read_text(encoding="utf-8")).values())
    races.sort(key=lambda r: (r["date"] or "", r["id"]))

    drv: dict[str, float] = {}
    con: dict[str, float] = {}
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
    return {"drivers": drv, "constructors": con, "quali": quali,
            "current_constructor": current_constructor,
            "name_to_id": name_to_id, "id_to_name": id_to_name}


def refresh_ratings():
    for series in SERIES:
        _cache[series] = _compute_series(series)


def _series_state(series: str) -> dict:
    if series not in _cache:
        _cache[series] = _compute_series(series)
    return _cache[series]


def resolve_driver_id(series: str, name: str) -> str | None:
    """Kalshi driver name -> our ESPN driver_id (normalized match)."""
    return _series_state(series)["name_to_id"].get(_norm(name))


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
