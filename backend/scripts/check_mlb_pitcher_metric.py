"""ERA vs FIP vs K-BB%: which starter metric actually carries the signal?

The MLB pitcher blend is the strongest signal in this app's MLB model
(era_diff r=0.305 against outcome, versus elo_diff's own 0.186), and it is
built on current-season ERA. ERA charges a pitcher for balls in play his
defence handled, so it is the noisiest of the common descriptors and regresses
hard. FIP and K-BB% use only what a pitcher controls nearly alone. If either
carries more signal, the single highest-leverage MLB improvement available is a
metric swap -- it costs no new data (every component comes back in the SAME
free StatsAPI call, see build_mlb_pitcher_fip_cache.py) and it improves
moneyline AND spread, since both read the same elo_diff.

METHOD IS DELIBERATELY THE INCUMBENT'S. Same game loop, same walk-forward Elo,
same MIN_IP gate, same strictly-before snapshot lookup, same per-season pooled
logistic as check_mlb_pitcher_signal.py -- so any difference is the METRIC, not
the harness. The incumbent's own numbers are re-derived here as the baseline
arm rather than quoted from its docstring, because a comparison against a
remembered number is not a comparison.

AND ONE THING THE INCUMBENT DOES NOT DO. Its per-season logistic is fit and
read IN-SAMPLE, which measures description, not prediction -- a noisier metric
can look competitive in-sample simply by having more room to fit. This adds a
walk-forward OUT-OF-SAMPLE arm: train on every prior season, score the held-out
one, and compare log-loss against an elo-only baseline. A metric that does not
beat elo-only out of sample does not belong in the model whatever its
correlation says.

SIGN CONVENTIONS, all "positive = home starter is better", so every coefficient
should come out POSITIVE and is directly comparable:
    era_diff = away_era - home_era      (lower ERA is better)
    fip_diff = away_fip - home_fip      (lower FIP is better)
    kbb_diff = home_kbb - away_kbb      (higher K-BB% is better)

FIP's league constant is omitted on purpose: it shifts every pitcher in a
season by the same amount and cancels exactly in a difference.

Run: backend/.venv/Scripts/python.exe scripts/check_mlb_pitcher_metric.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt  # noqa: E402
import json  # noqa: E402

import numpy as np  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from app.models.baseline.elo_mlb import (  # noqa: E402
    EloState, HOME_FIELD_ADV, NEUTRAL_SITE_HOME_FIELD_ADV, predict_and_update,
)

DATA = Path(__file__).resolve().parents[2] / "data"
SCHEDULE_PATH = DATA / "mlb_schedule_cache.json"
FIP_CACHE_PATH = DATA / "mlb_pitcher_fip_cache.json"
MIN_IP = 15.0          # identical to the incumbent's gate
OUTLIER_MULT = 3.0     # identical "cap, don't discard" treatment


def _snapshot_for(cache: dict, season: int, game_date: dt.date, pitcher_id: str) -> dict | None:
    """Latest snapshot STRICTLY BEFORE the game date. Same as the incumbent."""
    season_snaps = cache.get(str(season), {})
    best = None
    for date_str, snap in season_snaps.items():
        snap_date = dt.date.fromisoformat(date_str)
        if snap_date >= game_date:
            continue
        if best is None or snap_date > best[0]:
            best = (snap_date, snap)
    if best is None:
        return None
    return best[1].get(pitcher_id)


def _fip_raw(s: dict) -> float | None:
    ip = s.get("ip") or 0.0
    if ip <= 0:
        return None
    return (13.0 * s.get("hr", 0) + 3.0 * (s.get("bb", 0) + s.get("hbp", 0))
            - 2.0 * s.get("k", 0)) / ip


def _kbb(s: dict) -> float | None:
    bf = s.get("bf") or 0
    if bf <= 0:
        return None
    return (s.get("k", 0) - s.get("bb", 0)) / bf


def _league_means(cache: dict) -> tuple[float, float]:
    """League-average raw FIP and ERA over every qualifying snapshot line, used
    only to CAP small-sample outliers. Derived from the data rather than
    hardcoded, so it cannot silently go stale."""
    fips, eras = [], []
    for season_snaps in cache.values():
        for snap in season_snaps.values():
            for s in snap.values():
                if (s.get("ip") or 0) < MIN_IP:
                    continue
                f = _fip_raw(s)
                if f is not None:
                    fips.append(f)
                if s.get("era") is not None:
                    eras.append(s["era"])
    return float(np.mean(fips)), float(np.mean(eras))


def _logloss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def main() -> None:
    if not FIP_CACHE_PATH.exists():
        raise SystemExit(f"missing {FIP_CACHE_PATH} -- run build_mlb_pitcher_fip_cache.py first")

    games = json.loads(SCHEDULE_PATH.read_text())
    cache = json.loads(FIP_CACHE_PATH.read_text())
    mean_fip, mean_era = _league_means(cache)

    games = [g for g in games if g["game_type"] == "R" and g["season"] < 2026]
    games.sort(key=lambda g: (g["season"], g["gameday"], g["game_number"], g["id"]))

    state = EloState()
    rows = []
    skipped = {"no_pid": 0, "no_snapshot": 0, "low_ip": 0, "no_metric": 0}

    for g in games:
        hfa = NEUTRAL_SITE_HOME_FIELD_ADV if g.get("location") == "Neutral" else HOME_FIELD_ADV
        elo_diff = (state.get(g["home_team"]) + hfa) - state.get(g["away_team"])
        predict_and_update(state, g)  # walk forward regardless of qualification

        if g.get("home_score") is None or g.get("away_score") is None or g["home_score"] == g["away_score"]:
            continue
        home_pid, away_pid = g.get("home_probable_pitcher_id"), g.get("away_probable_pitcher_id")
        if not home_pid or not away_pid:
            skipped["no_pid"] += 1
            continue

        gd = dt.date.fromisoformat(g["gameday"])
        hs = _snapshot_for(cache, g["season"], gd, str(home_pid))
        as_ = _snapshot_for(cache, g["season"], gd, str(away_pid))
        if hs is None or as_ is None:
            skipped["no_snapshot"] += 1
            continue
        if hs["ip"] < MIN_IP or as_["ip"] < MIN_IP:
            skipped["low_ip"] += 1
            continue

        h_fip, a_fip = _fip_raw(hs), _fip_raw(as_)
        h_kbb, a_kbb = _kbb(hs), _kbb(as_)
        if None in (h_fip, a_fip, h_kbb, a_kbb):
            skipped["no_metric"] += 1
            continue

        cap_e, cap_f = mean_era * OUTLIER_MULT, abs(mean_fip) * OUTLIER_MULT + abs(mean_fip)
        era_diff = min(as_["era"], cap_e) - min(hs["era"], cap_e)
        fip_diff = min(a_fip, cap_f) - min(h_fip, cap_f)
        kbb_diff = h_kbb - a_kbb

        rows.append((g["season"], elo_diff, era_diff, fip_diff, kbb_diff,
                     1.0 if g["home_score"] > g["away_score"] else 0.0,
                     g["home_score"] - g["away_score"]))

    print(f"league-average raw FIP {mean_fip:+.3f}   league-average ERA {mean_era:.3f}")
    print(f"Qualifying games: {len(rows)}   skipped: {skipped}")
    print()

    seasons = sorted({r[0] for r in rows})
    arr = {name: np.array([r[i] for r in rows]) for i, name in enumerate(
        ["season", "elo", "era", "fip", "kbb", "outcome", "margin"])}
    y = arr["outcome"]

    print("RAW CORRELATIONS")
    print(f"{'metric':<8}{'vs outcome':>13}{'vs margin':>12}{'vs elo (redundancy)':>22}")
    for m in ("era", "fip", "kbb"):
        print(f"{m:<8}{np.corrcoef(arr[m], y)[0,1]:>13.4f}"
              f"{np.corrcoef(arr[m], arr['margin'])[0,1]:>12.4f}"
              f"{np.corrcoef(arr[m], arr['elo'])[0,1]:>22.4f}")
    print()

    print("PER-SEASON POOLED LOGISTIC (in-sample, the incumbent's own test)")
    print(f"{'metric':<8}{'mean coef':>12}{'std':>10}{'positive seasons':>20}{'coef ratio vs elo':>20}")
    for m in ("era", "fip", "kbb"):
        coefs, ratios = [], []
        for s in seasons:
            mask = arr["season"] == s
            X = StandardScaler().fit_transform(np.column_stack([arr["elo"][mask], arr[m][mask]]))
            clf = LogisticRegression().fit(X, y[mask])
            coefs.append(clf.coef_[0][1])
            if abs(clf.coef_[0][0]) > 1e-9:
                ratios.append(clf.coef_[0][1] / clf.coef_[0][0])
        coefs = np.array(coefs)
        print(f"{m:<8}{coefs.mean():>12.4f}{coefs.std():>10.4f}"
              f"{f'{(coefs > 0).sum()}/{len(coefs)}':>20}{np.mean(ratios):>20.4f}")
    print()

    print("WALK-FORWARD OUT-OF-SAMPLE (train on all prior seasons, score held-out)")
    print("lower log-loss is better; the elo-only column is the bar to beat")
    print(f"{'season':<8}{'elo only':>11}{'+era':>11}{'+fip':>11}{'+kbb':>11}{'n':>8}")
    totals = {k: [] for k in ("elo", "era", "fip", "kbb")}
    for s in seasons[1:]:
        tr, te = arr["season"] < s, arr["season"] == s
        if tr.sum() < 500 or te.sum() < 200:
            continue
        line = {}
        for name, cols in (("elo", ["elo"]), ("era", ["elo", "era"]),
                           ("fip", ["elo", "fip"]), ("kbb", ["elo", "kbb"])):
            Xtr = np.column_stack([arr[c][tr] for c in cols])
            Xte = np.column_stack([arr[c][te] for c in cols])
            sc = StandardScaler().fit(Xtr)
            clf = LogisticRegression().fit(sc.transform(Xtr), y[tr])
            ll = _logloss(clf.predict_proba(sc.transform(Xte))[:, 1], y[te])
            line[name] = ll
            totals[name].append(ll)
        print(f"{s:<8}{line['elo']:>11.5f}{line['era']:>11.5f}"
              f"{line['fip']:>11.5f}{line['kbb']:>11.5f}{te.sum():>8}")

    print()
    if totals["elo"]:
        base = np.mean(totals["elo"])
        print(f"mean out-of-sample log-loss -- elo only: {base:.5f}")
        for m in ("era", "fip", "kbb"):
            v = np.mean(totals[m])
            wins = sum(1 for a, b in zip(totals[m], totals["elo"]) if a < b)
            print(f"   +{m}: {v:.5f}   delta {v - base:+.5f}   beats elo-only in "
                  f"{wins}/{len(totals[m])} held-out seasons")
        print()
        print("VERDICT IS THE DELTA AND THE SEASON COUNT TOGETHER. A metric that")
        print("improves the mean on the back of one season is not a shippable signal;")
        print("this app has been bitten by exactly that (see the esports idle-decay and")
        print("goal-scale findings). Ship only on a consistent negative delta.")


if __name__ == "__main__":
    main()
