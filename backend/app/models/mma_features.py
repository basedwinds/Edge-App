"""Point-in-time UFC fighter features, built from ufcstats.com's real
per-fight stats (data/ufc_fight_cache.json) and static bio attributes
(data/ufc_fighter_bio_cache.json). Same "safe features" discipline the
earlier, separate ufc-model research project established for this exact
domain: every rolling stat is computed from a STRICT chronological shift
(only fights strictly BEFORE the one being featured), never the frozen
career-cumulative totals ufcstats' fighter-bio page itself exposes (those
are leakage -- see ufcstats_client.py's own docstring on why they're
excluded entirely from this scrape). A debut fighter's rolling stats are
left as None (unknown), never defaulted to 0 or a league-average guess.

Built for the went-the-distance model specifically (this app's flagship
differentiator market, per project_ufc_betting_model's earlier finding),
but the feature set is generic enough to reuse for other markets later.
"""
import datetime as dt
import re

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def parse_dob(dob: str | None) -> dt.date | None:
    if not dob or dob == "--":
        return None
    m = re.match(r"(\w{3})\s+(\d{1,2}),\s+(\d{4})", dob.strip())
    if not m:
        return None
    month_str, day, year = m.groups()
    month = _MONTHS.get(month_str)
    if not month:
        return None
    return dt.date(int(year), month, int(day))


def parse_height_inches(height: str | None) -> float | None:
    if not height or height == "--":
        return None
    m = re.match(r"(\d+)'\s*(\d+)\"", height.strip())
    if not m:
        return None
    feet, inches = m.groups()
    return int(feet) * 12 + int(inches)


def parse_reach_inches(reach: str | None) -> float | None:
    if not reach or reach == "--":
        return None
    m = re.match(r"(\d+(?:\.\d+)?)\"", reach.strip())
    return float(m.group(1)) if m else None


def parse_landed(stat: str | None) -> float | None:
    """"12 of 34" -> 12.0 (landed count -- the "of Y attempted" half is
    less useful for a rolling-volume feature than landed itself)."""
    if not stat:
        return None
    m = re.match(r"(\d+)\s+of\s+\d+", stat.strip())
    return float(m.group(1)) if m else None


def parse_control_seconds(ctrl: str | None) -> float | None:
    if not ctrl or ctrl == "--":
        return None
    m = re.match(r"(\d+):(\d{2})", ctrl.strip())
    if not m:
        return None
    minutes, seconds = m.groups()
    return int(minutes) * 60 + int(seconds)


_WEIGHT_DIVISIONS = (
    "Strawweight", "Flyweight", "Bantamweight", "Featherweight", "Lightweight",
    "Welterweight", "Middleweight", "Light Heavyweight", "Heavyweight",
)


def normalize_weight_class(weight_class: str | None) -> str:
    """ufcstats' raw weight_class field (parsed from the fight-title text)
    has 124 distinct strings across the full history -- mostly "UFC X"/
    "UFC Interim X" prefix variants of the SAME real division, plus early-
    UFC one-off tournament names ("UFC 1 Tournament", "Ultimate Ultimate
    '95 Tournament", etc, one fight each). Left raw, this fragments a
    one-hot encoding into dozens of near-empty columns -- caught live
    (2026-07-18) while validating weight_class as a method-of-finish
    feature: the fragmented version destabilized early small-training-set
    walk-forward folds badly (one fold's Brier blew up from 0.64 to 0.77),
    while this normalized version did not. Collapses every "UFC "/
    "Interim " prefix variant into its real division, buckets Open
    Weight/Catch Weight/tournament-era one-offs into a single
    "Open/Catch/Other" category, and leaves anything else "Unknown" (never
    guessed)."""
    if not weight_class:
        return "Unknown"
    wl = weight_class.lower()
    is_women = "women" in wl
    for division in _WEIGHT_DIVISIONS:
        if division.lower() in wl:
            return ("Women's " if is_women else "") + division
    if "open weight" in wl or "catch weight" in wl or "tournament" in wl or "superfight" in wl:
        return "Open/Catch/Other"
    return "Unknown"


def _method_bucket(method: str | None) -> str | None:
    """"Decision - Unanimous" -> "decision", "KO/TKO" -> "kotko",
    "Submission" -> "submission" -- None for DQ/overturned/other (genuinely
    ambiguous, not one of the 3 real outcomes, same set is_went_the_distance
    already treats as unknown)."""
    if not method:
        return None
    m = method.strip().lower()
    if m.startswith("decision"):
        return "decision"
    if "ko" in m or "tko" in m:
        return "kotko"
    if "submission" in m:
        return "submission"
    return None


class _RollingFighterState:
    __slots__ = (
        "n_fights", "n_wins", "n_losses", "n_finishes_for", "n_decisions_for",
        "n_own_fights_distance", "sum_sig_str_landed", "sum_td_landed",
        "last_fight_date", "n_ko_wins", "n_sub_wins", "n_ko_losses", "n_sub_losses",
    )

    def __init__(self):
        self.n_fights = 0
        self.n_wins = 0
        self.n_losses = 0
        self.n_finishes_for = 0  # wins by KO/TKO or Submission
        self.n_decisions_for = 0  # wins by decision
        self.n_own_fights_distance = 0  # fights (win or loss) that went the distance
        self.sum_sig_str_landed = 0.0
        self.sum_td_landed = 0.0
        self.last_fight_date: dt.date | None = None
        self.n_ko_wins = 0  # wins specifically by KO/TKO (method_of_finish features)
        self.n_sub_wins = 0  # wins specifically by submission
        self.n_ko_losses = 0  # times THIS fighter was finished by KO/TKO -- a real durability/chin proxy
        self.n_sub_losses = 0

    def snapshot(self, as_of: dt.date | None) -> dict:
        n = self.n_fights
        layoff_days = (as_of - self.last_fight_date).days if (as_of and self.last_fight_date) else None
        return {
            "experience": n,
            "win_rate": (self.n_wins / n) if n > 0 else None,
            "finish_rate": (self.n_finishes_for / self.n_wins) if self.n_wins > 0 else None,
            "went_distance_rate": (self.n_own_fights_distance / n) if n > 0 else None,
            "avg_sig_str_landed": (self.sum_sig_str_landed / n) if n > 0 else None,
            "avg_td_landed": (self.sum_td_landed / n) if n > 0 else None,
            "layoff_days": layoff_days,
            "ko_win_rate": (self.n_ko_wins / n) if n > 0 else None,
            "sub_win_rate": (self.n_sub_wins / n) if n > 0 else None,
            "ko_loss_rate": (self.n_ko_losses / n) if n > 0 else None,
            "sub_loss_rate": (self.n_sub_losses / n) if n > 0 else None,
        }

    def apply_result(self, fight_date: dt.date | None, won: bool | None, method: str | None, went_distance: int | None, sig_str_landed: float | None, td_landed: float | None):
        """won: True/False/None (None = draw -- counts toward experience and
        went-the-distance history, but not win/loss/finish tallies)."""
        if sig_str_landed is not None:
            self.sum_sig_str_landed += sig_str_landed
        if td_landed is not None:
            self.sum_td_landed += td_landed
        self.n_fights += 1
        bucket = _method_bucket(method)
        if won is True:
            self.n_wins += 1
            if method:
                if bucket == "decision":
                    self.n_decisions_for += 1
                else:
                    self.n_finishes_for += 1
            if bucket == "kotko":
                self.n_ko_wins += 1
            elif bucket == "submission":
                self.n_sub_wins += 1
        elif won is False:
            self.n_losses += 1
            if bucket == "kotko":
                self.n_ko_losses += 1
            elif bucket == "submission":
                self.n_sub_losses += 1
        if went_distance == 1:
            self.n_own_fights_distance += 1
        if fight_date:
            self.last_fight_date = fight_date


def _mean_ignore_none(a: float | None, b: float | None) -> float | None:
    vals = [v for v in (a, b) if v is not None]
    return sum(vals) / len(vals) if vals else None


def _max_ignore_none(a: float | None, b: float | None) -> float | None:
    vals = [v for v in (a, b) if v is not None]
    return max(vals) if vals else None


# Every symmetric numeric feature the went-the-distance model uses --
# shared list so the live-serving path (distance_service_mma.py) and the
# offline backtest (scripts/backtest_mma_distance.py) can NEVER silently
# drift apart on what "the feature set" means.
DISTANCE_MODEL_NUMERIC_FEATURES = [
    "combined_experience", "combined_win_rate", "combined_finish_rate", "max_finish_rate",
    "combined_went_distance_rate", "combined_sig_str_landed", "max_sig_str_landed",
    "combined_td_landed", "max_layoff_days", "combined_age", "combined_reach", "reach_diff_abs",
    "scheduled_rounds", "is_title_bout",
]


def to_symmetric_distance_features(r: dict) -> dict:
    """Went-the-distance is symmetric in fighter_a/fighter_b (see module
    docstring) -- converts one build_feature_rows()-shaped row into
    ORDER-INVARIANT combined features (mean/max of both fighters' rolling
    stats), the actual model input. Returns a flat dict with every key in
    DISTANCE_MODEL_NUMERIC_FEATURES plus "weight_class" (still categorical,
    one-hot'd by the caller) -- does NOT include "went_the_distance" or
    "year"/"event_date"/"fight_id", which only the training path needs."""
    return {
        "combined_experience": _mean_ignore_none(r["a_experience"], r["b_experience"]),
        "combined_win_rate": _mean_ignore_none(r["a_win_rate"], r["b_win_rate"]),
        "combined_finish_rate": _mean_ignore_none(r["a_finish_rate"], r["b_finish_rate"]),
        "max_finish_rate": _max_ignore_none(r["a_finish_rate"], r["b_finish_rate"]),
        "combined_went_distance_rate": _mean_ignore_none(r["a_went_distance_rate"], r["b_went_distance_rate"]),
        "combined_sig_str_landed": _mean_ignore_none(r["a_avg_sig_str_landed"], r["b_avg_sig_str_landed"]),
        "max_sig_str_landed": _max_ignore_none(r["a_avg_sig_str_landed"], r["b_avg_sig_str_landed"]),
        "combined_td_landed": _mean_ignore_none(r["a_avg_td_landed"], r["b_avg_td_landed"]),
        "max_layoff_days": _max_ignore_none(r["a_layoff_days"], r["b_layoff_days"]),
        "combined_age": _mean_ignore_none(r["a_age"], r["b_age"]),
        "combined_reach": _mean_ignore_none(r["a_reach_in"], r["b_reach_in"]),
        "reach_diff_abs": abs(r["a_reach_in"] - r["b_reach_in"]) if (r["a_reach_in"] is not None and r["b_reach_in"] is not None) else None,
        "scheduled_rounds": r["scheduled_rounds"],
        "is_title_bout": r["is_title_bout"],
        "weight_class": normalize_weight_class(r["weight_class"]),
    }


# Method of finish (KO/TKO vs Submission vs Decision) is ALSO symmetric in
# fighter_a/fighter_b -- same reasoning as the distance model. Validated
# real (scripts/check_mma_method_signal.py): Brier beats a naive base-rate
# baseline in 17/17 yearly walk-forward folds.
METHOD_MODEL_NUMERIC_FEATURES = [
    "combined_ko_win_rate", "combined_sub_win_rate", "combined_ko_loss_rate", "combined_sub_loss_rate",
    "max_ko_win_rate", "max_sub_win_rate", "combined_experience", "scheduled_rounds",
]


def to_symmetric_method_features(r: dict) -> dict:
    """Returns a flat dict with every key in METHOD_MODEL_NUMERIC_FEATURES,
    plus "weight_class" (categorical, one-hot'd by the caller -- same
    pattern as to_symmetric_distance_features). Age/reach/is_title_bout
    are still deliberately excluded -- scripts/check_mma_method_signal.py
    only validated the leaner numeric set above; weight_class was added
    2026-07-18 after scripts/check_mma_round2_signals.py validated it as a
    real, separate improvement (heavier divisions finish more often, a
    well-documented power effect) via a real walk-forward Brier check
    (0.6048 -> 0.6003, beat the leaner model in 13/17 yearly folds) --
    checked-and-confirmed like every other feature here, not assumed to
    transfer from distance's own validated use of the same field."""
    return {
        "combined_ko_win_rate": _mean_ignore_none(r["a_ko_win_rate"], r["b_ko_win_rate"]),
        "combined_sub_win_rate": _mean_ignore_none(r["a_sub_win_rate"], r["b_sub_win_rate"]),
        "combined_ko_loss_rate": _mean_ignore_none(r["a_ko_loss_rate"], r["b_ko_loss_rate"]),
        "combined_sub_loss_rate": _mean_ignore_none(r["a_sub_loss_rate"], r["b_sub_loss_rate"]),
        "max_ko_win_rate": _max_ignore_none(r["a_ko_win_rate"], r["b_ko_win_rate"]),
        "max_sub_win_rate": _max_ignore_none(r["a_sub_win_rate"], r["b_sub_win_rate"]),
        "combined_experience": _mean_ignore_none(r["a_experience"], r["b_experience"]),
        "scheduled_rounds": r["scheduled_rounds"],
        "weight_class": normalize_weight_class(r["weight_class"]),
    }


# Round-of-finish (1..scheduled_rounds, where a decision's round IS
# scheduled_rounds) -- extends BOTH the distance and method work, so reuses
# the union of their feature sets rather than inventing a third list.
# Real, but weaker/noisier signal than either (scripts/check_mma_rounds_
# signal.py): the raw 5-way round target only beat a per-scheduled_rounds
# naive baseline in 13/17 yearly Brier folds (vs distance/method's 17/17),
# and LOST on accuracy in most years. The market-relevant question --
# summed P(ends before round N) for each real ladder rung -- is more
# robust (10-15/17 Brier wins depending on rung, always net-positive in
# total) since per-class errors partially cancel when summed, but still
# ships with an explicit "noisier than this app's other MMA signals"
# caveat everywhere it's surfaced -- see rounds_service_mma.py.
ROUNDS_MODEL_NUMERIC_FEATURES = sorted(set(DISTANCE_MODEL_NUMERIC_FEATURES + METHOD_MODEL_NUMERIC_FEATURES) - {"weight_class"})


def to_symmetric_rounds_features(r: dict) -> dict:
    """Union of to_symmetric_distance_features and to_symmetric_method_features's
    numeric outputs (weight_class dropped -- categorical, not worth one-hot'ing
    for this weaker signal). Both source dicts already compute every key
    ROUNDS_MODEL_NUMERIC_FEATURES needs, so this is a plain merge."""
    merged = {**to_symmetric_distance_features(r), **to_symmetric_method_features(r)}
    merged.pop("weight_class", None)
    return merged


def build_feature_rows(fights: list[dict], raw_rows: list[dict], bios: dict[str, dict]) -> list[dict]:
    """One row per fight with PRE-fight features for both fighters + the
    went_the_distance target, in a single chronological pass -- each row is
    built from state as of just before the fight, then that fighter's state
    is updated with the REAL result before moving to the next (later) fight.
    `fights` must already be chronologically sorted (ufc_data.load_fights
    guarantees this). Skips fights with no clean target (no-contests) for
    training, but no-contests still update last_fight_date (a real, if
    unresolved, appearance shouldn't be invisible to the layoff feature)."""
    raw_by_fight_fighter: dict[tuple[str, str], dict] = {}
    for row in raw_rows:
        fid = row["fight_url"].rstrip("/").rsplit("/", 1)[-1]
        raw_by_fight_fighter[(fid, row["fighter_id"])] = row

    state: dict[str, _RollingFighterState] = {}

    def get_state(fighter_id: str) -> _RollingFighterState:
        if fighter_id not in state:
            state[fighter_id] = _RollingFighterState()
        return state[fighter_id]

    rows = []
    for f in fights:
        fight_id = f["id"]
        fight_date = dt.date.fromisoformat(f["event_date"]) if f["event_date"] else None
        has_target = f["went_the_distance"] is not None and not f["is_no_contest"]

        if has_target:
            a_state, b_state = get_state(f["fighter_a_id"]), get_state(f["fighter_b_id"])
            a_snap, b_snap = a_state.snapshot(fight_date), b_state.snapshot(fight_date)

            a_bio, b_bio = bios.get(f["fighter_a_id"], {}), bios.get(f["fighter_b_id"], {})
            a_dob, b_dob = parse_dob(a_bio.get("dob")), parse_dob(b_bio.get("dob"))
            a_age = (fight_date - a_dob).days / 365.25 if (fight_date and a_dob) else None
            b_age = (fight_date - b_dob).days / 365.25 if (fight_date and b_dob) else None

            rows.append({
                "fight_id": fight_id,
                "event_date": f["event_date"],
                "weight_class": f["weight_class"],
                "is_title_bout": f["is_title_bout"],
                "scheduled_rounds": f["scheduled_rounds"],
                "a_experience": a_snap["experience"], "b_experience": b_snap["experience"],
                "a_win_rate": a_snap["win_rate"], "b_win_rate": b_snap["win_rate"],
                "a_finish_rate": a_snap["finish_rate"], "b_finish_rate": b_snap["finish_rate"],
                "a_went_distance_rate": a_snap["went_distance_rate"], "b_went_distance_rate": b_snap["went_distance_rate"],
                "a_avg_sig_str_landed": a_snap["avg_sig_str_landed"], "b_avg_sig_str_landed": b_snap["avg_sig_str_landed"],
                "a_avg_td_landed": a_snap["avg_td_landed"], "b_avg_td_landed": b_snap["avg_td_landed"],
                "a_layoff_days": a_snap["layoff_days"], "b_layoff_days": b_snap["layoff_days"],
                "a_age": a_age, "b_age": b_age,
                "a_height_in": parse_height_inches(a_bio.get("height")), "b_height_in": parse_height_inches(b_bio.get("height")),
                "a_reach_in": parse_reach_inches(a_bio.get("reach")), "b_reach_in": parse_reach_inches(b_bio.get("reach")),
                "a_ko_win_rate": a_snap["ko_win_rate"], "b_ko_win_rate": b_snap["ko_win_rate"],
                "a_sub_win_rate": a_snap["sub_win_rate"], "b_sub_win_rate": b_snap["sub_win_rate"],
                "a_ko_loss_rate": a_snap["ko_loss_rate"], "b_ko_loss_rate": b_snap["ko_loss_rate"],
                "a_sub_loss_rate": a_snap["sub_loss_rate"], "b_sub_loss_rate": b_snap["sub_loss_rate"],
                "went_the_distance": f["went_the_distance"],
                "method_bucket": _method_bucket(f.get("method")),
            })

        if f["is_no_contest"]:
            if fight_date:
                get_state(f["fighter_a_id"]).last_fight_date = fight_date
                get_state(f["fighter_b_id"]).last_fight_date = fight_date
            continue

        a_raw = raw_by_fight_fighter.get((fight_id, f["fighter_a_id"]))
        b_raw = raw_by_fight_fighter.get((fight_id, f["fighter_b_id"]))
        a_sig = parse_landed(a_raw.get("Sig. str.")) if a_raw else None
        b_sig = parse_landed(b_raw.get("Sig. str.")) if b_raw else None
        a_td = parse_landed(a_raw.get("Td")) if a_raw else None
        b_td = parse_landed(b_raw.get("Td")) if b_raw else None

        if f["is_draw"]:
            get_state(f["fighter_a_id"]).apply_result(fight_date, None, f.get("method"), f["went_the_distance"], a_sig, a_td)
            get_state(f["fighter_b_id"]).apply_result(fight_date, None, f.get("method"), f["went_the_distance"], b_sig, b_td)
        elif f["winner_id"] is not None:
            winner_is_a = f["winner_id"] == f["fighter_a_id"]
            get_state(f["fighter_a_id"]).apply_result(fight_date, winner_is_a, f.get("method"), f["went_the_distance"], a_sig, a_td)
            get_state(f["fighter_b_id"]).apply_result(fight_date, not winner_is_a, f.get("method"), f["went_the_distance"], b_sig, b_td)

    return rows


def compute_current_snapshots(fights: list[dict], raw_rows: list[dict], as_of: dt.date | None = None) -> dict[str, dict]:
    """Same rolling pass as build_feature_rows, but only cares about each
    fighter's FINAL state after the last fight in `fights` -- used for LIVE
    scoring of an upcoming fight (need each fighter's rolling stats as of
    right now, not a specific past fight's pre-fight snapshot). Shares
    _RollingFighterState's update logic so both paths stay in sync. `as_of`
    should be the UPCOMING fight's own date (or today) so layoff_days is
    computed relative to that, not left None."""
    raw_by_fight_fighter: dict[tuple[str, str], dict] = {}
    for row in raw_rows:
        fid = row["fight_url"].rstrip("/").rsplit("/", 1)[-1]
        raw_by_fight_fighter[(fid, row["fighter_id"])] = row

    state: dict[str, _RollingFighterState] = {}

    def get_state(fighter_id: str) -> _RollingFighterState:
        if fighter_id not in state:
            state[fighter_id] = _RollingFighterState()
        return state[fighter_id]

    for f in fights:
        fight_id = f["id"]
        fight_date = dt.date.fromisoformat(f["event_date"]) if f["event_date"] else None

        if f["is_no_contest"]:
            if fight_date:
                get_state(f["fighter_a_id"]).last_fight_date = fight_date
                get_state(f["fighter_b_id"]).last_fight_date = fight_date
            continue

        a_raw = raw_by_fight_fighter.get((fight_id, f["fighter_a_id"]))
        b_raw = raw_by_fight_fighter.get((fight_id, f["fighter_b_id"]))
        a_sig = parse_landed(a_raw.get("Sig. str.")) if a_raw else None
        b_sig = parse_landed(b_raw.get("Sig. str.")) if b_raw else None
        a_td = parse_landed(a_raw.get("Td")) if a_raw else None
        b_td = parse_landed(b_raw.get("Td")) if b_raw else None

        if f["is_draw"]:
            get_state(f["fighter_a_id"]).apply_result(fight_date, None, f.get("method"), f["went_the_distance"], a_sig, a_td)
            get_state(f["fighter_b_id"]).apply_result(fight_date, None, f.get("method"), f["went_the_distance"], b_sig, b_td)
        elif f["winner_id"] is not None:
            winner_is_a = f["winner_id"] == f["fighter_a_id"]
            get_state(f["fighter_a_id"]).apply_result(fight_date, winner_is_a, f.get("method"), f["went_the_distance"], a_sig, a_td)
            get_state(f["fighter_b_id"]).apply_result(fight_date, not winner_is_a, f.get("method"), f["went_the_distance"], b_sig, b_td)

    return {fid: s.snapshot(as_of=as_of) for fid, s in state.items()}
