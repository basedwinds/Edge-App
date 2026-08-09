"""Walk-forward accuracy of a plain Elo on Call of Duty. The go/no-go gate.

THE ONLY QUESTION: replaying every match in date order, with each rating built
strictly from matches that finished BEFORE the one being predicted, does Elo
beat a coin flip by enough to be worth a sport?

The bar the shipped titles cleared on the identical test: CS2 60.75%, LoL
67.13%. A title that lands near 50% is noise no matter how clean its data is.

WHY WALK-FORWARD AND NOT A SPLIT. Ratings are cumulative, so a random split
leaks: a team's rating in the training set already encodes the test matches it
played. Replaying chronologically is the only version of this test that says
anything about live performance.

WARM-UP IS EXCLUDED FROM SCORING. The first matches are all 1500-vs-1500 coin
flips that no model could get right, and counting them drags every model toward
50% equally. Predictions are only scored once BOTH teams have MIN_GAMES of
history -- the same minimum-games discipline the esports titles already use.

Reported alongside accuracy: log-loss and Brier, because accuracy alone cannot
tell a confident-and-right model from a lucky one, and a market price needs
calibration rather than a correct side.

===========================================================================
RESULT, 2026-08-09: PASSES. 3,614 decided matches, 2020-01-24 .. 2026-08-08.

     K   scored  accuracy  log-loss    Brier
    16     2508    0.6479    0.6374   0.2231   <- best
    24     2508    0.6459    0.6334   0.2212
    32     2508    0.6443    0.6323   0.2206
    40     2508    0.6459    0.6328   0.2207

+14.79pp over a coin flip, z = 14.8. That lands BETWEEN the two shipped
titles (CS2 0.6075, LoL 0.6713), so Call of Duty is as predictable from plain
team Elo as the esports this app already prices.

Accuracy is flat across K (0.6443-0.6479) while log-loss prefers K=32. Flat
means the result is not a K-tuning artefact, which is the thing worth knowing
-- a signal that only exists at one K usually is not one.

FACE VALIDITY, which matters as much as the number here because the data
source is new: the top two rated franchises are OpTic Texas (1705.9, 257
matches) and Atlanta FaZe (1704.8, 279) -- the two dominant CDL teams of the
era, recovered without being told. A source that had mis-joined teams would
not produce that ordering.

CAVEAT BEFORE PRICING. This says the RATINGS predict; it does not say there is
an EDGE. Every sport in this app measures ~0 average edge against the market,
and the only honest next test is the same one CS2 and LoL had to pass:
backtest against real Kalshi/Polymarket CoD odds. There were no live CoD
markets during this session's catalog sweeps, so that test is still owed --
and supply, not accuracy, is what will decide whether CoD ships.
===========================================================================
"""
from __future__ import annotations

import collections
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CACHE = Path(__file__).resolve().parents[2] / "data" / "cod_historical_match_cache.json"

BASE_RATING = 1500.0
MIN_GAMES = 5          # both teams, before a match is scored
K_GRID = (16, 24, 32, 40)


def expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))


def walk_forward(matches: list[dict], k: float, min_games: int):
    rating: dict[str, float] = collections.defaultdict(lambda: BASE_RATING)
    played: dict[str, int] = collections.Counter()
    correct = n = 0
    lls, briers, probs = [], [], []

    for m in matches:
        a, b = m["team_a"], m["team_b"]
        ra, rb = rating[a], rating[b]
        p = expected(ra, rb)
        won = 1 if m["score_a"] > m["score_b"] else 0

        # Score BEFORE updating, and only once both sides are out of warm-up.
        if played[a] >= min_games and played[b] >= min_games:
            n += 1
            correct += int((p > 0.5) == bool(won))
            pc = min(max(p, 1e-6), 1 - 1e-6)
            lls.append(-(won * math.log(pc) + (1 - won) * math.log(1 - pc)))
            briers.append((p - won) ** 2)
            probs.append(p)

        rating[a] = ra + k * (won - p)
        rating[b] = rb + k * ((1 - won) - (1 - p))
        played[a] += 1
        played[b] += 1

    return {
        "n": n,
        "acc": correct / n if n else 0.0,
        "logloss": statistics.mean(lls) if lls else 0.0,
        "brier": statistics.mean(briers) if briers else 0.0,
        "mean_p": statistics.mean(probs) if probs else 0.0,
        "ratings": rating,
        "played": played,
    }


def main() -> None:
    if not CACHE.exists():
        print(f"no cache at {CACHE} -- run build_cod_match_cache_bp.py first")
        return
    matches = json.loads(CACHE.read_text(encoding="utf-8"))
    matches = [m for m in matches if m.get("team_a") and m.get("team_b")
               and m.get("score_a") is not None and m.get("score_b") is not None
               and m["score_a"] != m["score_b"]]
    matches.sort(key=lambda m: (m["match_date"], m.get("datetime") or "", m["source_match_id"]))
    print(f"{len(matches)} decided matches, {matches[0]['match_date']} .. {matches[-1]['match_date']}")

    by_season = collections.Counter(m.get("season") for m in matches)
    print("per season:", dict(sorted(by_season.items())))
    print()

    print(f"{'K':>4s}{'scored':>9s}{'accuracy':>10s}{'log-loss':>10s}{'Brier':>9s}")
    best = None
    for k in K_GRID:
        r = walk_forward(matches, k, MIN_GAMES)
        print(f"{k:>4d}{r['n']:>9d}{r['acc']:>10.4f}{r['logloss']:>10.4f}{r['brier']:>9.4f}")
        if best is None or r["acc"] > best[1]["acc"]:
            best = (k, r)

    k, r = best
    print()
    print(f"BASELINE  coin flip                acc 0.5000  log-loss 0.6931  Brier 0.2500")
    print(f"BEST      K={k:<3d} ({r['n']} scored)      acc {r['acc']:.4f}  "
          f"log-loss {r['logloss']:.4f}  Brier {r['brier']:.4f}")
    print()
    print(f"vs coin flip: {(r['acc'] - 0.5) * 100:+.2f} pp accuracy, "
          f"{0.6931 - r['logloss']:+.4f} log-loss, {0.25 - r['brier']:+.4f} Brier")
    print("for scale, the same test on shipped titles: CS2 0.6075, LoL 0.6713")

    # A binomial sanity check -- 3,000 matches makes small edges significant,
    # but stating it beats eyeballing "looks better than 50%".
    if r["n"]:
        se = math.sqrt(0.25 / r["n"])
        z = (r["acc"] - 0.5) / se
        print(f"z vs 0.5 = {z:.1f} (se {se:.4f})")

    print()
    top = sorted(((t, v) for t, v in r["ratings"].items() if r["played"][t] >= 20),
                 key=lambda kv: -kv[1])[:12]
    print("top rated teams with >=20 matches (a face-validity check, not a result):")
    for t, v in top:
        print(f"   {t:32s} {v:7.1f}  ({r['played'][t]} matches)")


if __name__ == "__main__":
    main()
