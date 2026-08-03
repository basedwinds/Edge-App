"""12-team College Football Playoff Monte Carlo -- prices KXNCAAFPLAYOFF (make
the field), KXNCAAFQF (reach the quarterfinals) and KXNCAAFCONF (which
conference wins the national title).

READ THIS BEFORE TRUSTING THE NUMBERS. Unlike the win-total and conference sims,
this one models a HUMAN PROCESS. The real field is chosen by a selection
committee whose ranking is not a published formula, so the seeding here is an
explicit PROXY: teams are ordered by simulated wins, with Elo breaking ties.
That captures the two things the committee demonstrably weights most -- record
first, quality second -- but it cannot reproduce the committee's actual
judgement about schedule strength, injuries, or the eye test.

Everything below the seeding is real structure, not proxy:
  * 5 automatic bids to the highest-ranked conference champions, 7 at-large;
  * seeds 1-4 receive first-round byes;
  * first round 5v12, 6v11, 7v10, 8v9; winners join the byes in the
    quarterfinals; then semis and final, every game decided by Elo at a neutral
    site.

Because the seeding is a proxy, these markets should ship as APPROXIMATE and are
a poor candidate for staking -- the same posture as the F1 championship sim and
the esports tournament sim, which are priced and shown but not staked. The error
is concentrated exactly on the bubble teams, which is where the disagreement with
the market (and so the apparent edge) will look largest.

A SECOND, INDEPENDENT REASON TO DISTRUST THE TITLE ODDS. elo_cfb runs K=100,
which is correct for single-game prediction (it was the independent Brier
minimum on two held-out seasons) but produces a very WIDE rating spread. A
four-round bracket multiplies that spread, so the top-rated team comes out far
more dominant than a real market would price: the first run of this sim gave the
highest-rated team a 40.5% national title probability, where sportsbooks
typically top out nearer 15-20%. The per-round probabilities are internally
consistent -- they just compound an over-confident input. Treat title and
title-by-conference numbers as directionally useful and materially
over-concentrated at the top; the playoff-qualification markets, which need only
one "round", are much less affected.

Runs one unified pass because the playoff needs each trial's OVERALL record and
that trial's CONFERENCE CHAMPIONS together -- taking them from two independent
simulations would pair a team's good season with someone else's, inflating the
tails.
"""
import logging
import threading
import time

import numpy as np

from app.models import season_sim_cfb
from app.models.baseline import elo_cfb, elo_service_cfb

log = logging.getLogger("playoff_sim_cfb")

FIELD_SIZE = 12
AUTO_BIDS = 5           # highest-ranked conference champions
BYES = 4                # seeds 1-4 skip the first round

_TTL = 3600
# Retry window for a FAILED run (empty result) -- see warm().
_FAILURE_TTL = 120
_lock = threading.Lock()
_cache: dict = {}


def _neutral_win_prob(r_a: np.ndarray, r_b: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + 10 ** (-(r_a - r_b) / 400.0))


def simulate(trials: int = 4000, games: list[dict] | None = None) -> dict:
    games = season_sim_cfb._fetch_season_games() if games is None else games
    conf_of = season_sim_cfb.load_conferences()
    if not games or not conf_of:
        return {"playoff": {}, "quarterfinal": {}, "title": {}, "title_by_conference": {}}

    teams = sorted({t for g in games for t in (g["home_team"], g["away_team"])} & set(conf_of))
    if len(teams) < FIELD_SIZE:
        return {"playoff": {}, "quarterfinal": {}, "title": {}, "title_by_conference": {}}
    idx = {t: i for i, t in enumerate(teams)}
    n_teams = len(teams)
    strength = np.array([(elo_service_cfb.rating(t) or elo_cfb.BASE_RATING) for t in teams])

    # ---- one pass over the schedule, tracking overall AND conference wins ----
    banked_all = np.zeros(n_teams, dtype=np.int32)
    banked_conf = np.zeros(n_teams, dtype=np.int32)
    probs, pair, is_conf = [], [], []
    for g in games:
        h, a = g["home_team"], g["away_team"]
        if h not in idx or a not in idx:
            # One side is FCS/independent-only: it still affects the FBS team's
            # overall record, handled below via the FCS constant.
            solo = h if h in idx else (a if a in idx else None)
            if solo is None or g.get("home_score") is not None:
                if solo is not None and g.get("home_score") is not None:
                    won = (g["home_score"] > g["away_score"]) == (solo == h)
                    banked_all[idx[solo]] += int(won)
                continue
            probs.append(season_sim_cfb.FCS_WIN_PROB if solo == h else 1 - season_sim_cfb.FCS_WIN_PROB)
            pair.append((idx[solo], -1)); is_conf.append(False)
            continue
        same_conf = conf_of[h] == conf_of[a]
        if g.get("home_score") is not None and g.get("away_score") is not None:
            w = idx[h] if g["home_score"] > g["away_score"] else idx[a]
            banked_all[w] += 1
            if same_conf:
                banked_conf[w] += 1
            continue
        if not (elo_service_cfb.is_rated(h) and elo_service_cfb.is_rated(a)):
            continue
        p = elo_service_cfb.get_home_win_prob(h, a, bool(g.get("neutral")))
        if p is None:
            continue
        probs.append(float(p)); pair.append((idx[h], idx[a])); is_conf.append(same_conf)

    p_arr = np.array(probs) if probs else np.zeros(0)
    hi = np.array([x[0] for x in pair]) if pair else np.zeros(0, dtype=int)
    ai = np.array([x[1] for x in pair]) if pair else np.zeros(0, dtype=int)
    conf_mask = np.array(is_conf) if is_conf else np.zeros(0, dtype=bool)
    rng = np.random.default_rng()

    conf_ids = sorted({conf_of[t] for t in teams})
    conf_index = np.array([conf_ids.index(conf_of[t]) for t in teams])

    made = np.zeros(n_teams); qf = np.zeros(n_teams); title = np.zeros(n_teams)
    title_conf = np.zeros(len(conf_ids))

    chunk = 250
    done = 0
    while done < trials:
        n = min(chunk, trials - done)
        wins_all = np.tile(banked_all, (n, 1)).astype(np.int32)
        wins_conf = np.tile(banked_conf, (n, 1)).astype(np.int32)
        if len(p_arr):
            hw = rng.random((n, len(p_arr))) < p_arr
            valid = ai >= 0
            np.add.at(wins_all, (slice(None), hi), hw)
            np.add.at(wins_all, (slice(None), ai[valid]), ~hw[:, valid])
            cm = conf_mask & valid
            if cm.any():
                np.add.at(wins_conf, (slice(None), hi[cm]), hw[:, cm])
                np.add.at(wins_conf, (slice(None), ai[cm]), ~hw[:, cm])

        # committee proxy: record first, Elo as the tiebreak
        tie = (strength - strength.min()) / (float(np.ptp(strength)) + 1.0) * 0.9
        rank_score = wins_all + tie

        # conference champion = best conference record in that same trial
        champ_score = wins_conf + tie
        is_champ = np.zeros((n, n_teams), dtype=bool)
        for ci in range(len(conf_ids)):
            cols = np.where(conf_index == ci)[0]
            if len(cols) == 0:
                continue
            best = cols[np.argmax(champ_score[:, cols], axis=1)]
            is_champ[np.arange(n), best] = True

        order = np.argsort(-rank_score, axis=1)
        for s in range(n):
            row = order[s]
            champs = [t for t in row if is_champ[s, t]][:AUTO_BIDS]
            field = list(champs)
            for t in row:
                if len(field) >= FIELD_SIZE:
                    break
                if t not in field:
                    field.append(t)
            # Seeded by the same ranking, auto-bids included on merit order.
            field.sort(key=lambda t: -rank_score[s, t])
            made[field] += 1

            byes = field[:BYES]
            rest = field[BYES:]
            # 5v12, 6v11, 7v10, 8v9
            winners = []
            for k in range(len(rest) // 2):
                a_, b_ = rest[k], rest[len(rest) - 1 - k]
                pa = _neutral_win_prob(strength[a_], strength[b_])
                winners.append(a_ if rng.random() < pa else b_)
            bracket = byes + winners
            qf[bracket] += 1
            while len(bracket) > 1:
                nxt = []
                for k in range(len(bracket) // 2):
                    a_, b_ = bracket[k], bracket[len(bracket) - 1 - k]
                    pa = _neutral_win_prob(strength[a_], strength[b_])
                    nxt.append(a_ if rng.random() < pa else b_)
                bracket = nxt
            champ = bracket[0]
            title[champ] += 1
            title_conf[conf_index[champ]] += 1
        done += n

    return {
        "playoff": {teams[i]: made[i] / trials for i in range(n_teams)},
        "quarterfinal": {teams[i]: qf[i] / trials for i in range(n_teams)},
        "title": {teams[i]: title[i] / trials for i in range(n_teams)},
        "title_by_conference": {conf_ids[i]: title_conf[i] / trials for i in range(len(conf_ids))},
    }


def warm(trials: int = 2000) -> None:
    """Fewer trials than the other sims by default: this one has a Python loop
    per simulated season (the bracket is sequential), so it costs far more per
    trial than the vectorised win sim."""
    now = time.time()
    with _lock:
        hit = _cache.get("data")
    # A FAILED run must not latch: caching an empty result under the normal
    # _TTL would pin every season row to "not warm yet" for a full hour even
    # though the next attempt would likely succeed. Same fix as
    # season_sim_wnba.warm, where this was observed live (2026-08-03).
        if hit and now - hit[0] < (_TTL if hit[1] else _FAILURE_TTL):
            return
    try:
        data = simulate(trials=trials)
    except Exception:
        log.exception("cfb playoff sim failed")
        data = {}
    with _lock:
        _cache["data"] = (now, data)
    log.info("cfb playoff sim: %d teams, %d trials", len(data.get("playoff") or {}), trials)


def get() -> dict:
    with _lock:
        hit = _cache.get("data")
    return hit[1] if hit else {}
