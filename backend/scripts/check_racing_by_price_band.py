"""Should motorsport stake flat 1 unit at every price, including longshots?

THE QUESTION. Racing bets are sized by edge tier and ignore price entirely, so a
2% longshot and an 85% top-20 both get one unit. Two competing worries, neither
previously measured:

  (a) Flat DOLLAR staking is worst on FAVOURITES. $10 on an 85% top-20 risks $10
      to win about $1.76, so roughly 6 losses erase 34 wins.
  (b) The pre-qualifying model is FLAT -- measured on the 2026 Iowa Xfinity race,
      the top driver priced at 6.5% against a 21.5% market with 34 drivers spread
      almost evenly. A flat model against a peaked market manufactures positive
      edges across the entire longshot tail.

A longshot price floor was already TESTED AND REJECTED for futures, so it must
not be assumed to transfer here.

TWO MEASUREMENT TRAPS, both hit before this script existed:

  1. PAPER vs REAL. Racing has 44 settled bets but only THREE are real; the rest
     are paper. Mixing them and reporting the result as if it described the
     tracker is wrong. This uses the paper sample deliberately and says so --
     paper is the only sample large enough to say anything, and the question
     ("does the model's edge convert at this price?") is about the model, not
     about realised bankroll.

  2. ZERO-STAKE ROWS. paper_logger logs edged-but-unstaked rows so they accrue
     CLV, with stake_dollars = 0.0. Those cannot contribute to a dollar return
     and silently drag any naive ROI toward zero. Rather than drop them -- they
     are most of the sample and they carry a real price and a real outcome --
     every row is valued at a NOTIONAL FLAT STAKE. That answers the actual
     question, which is whether flat staking works per price band, and uses the
     whole sample instead of the handful that happened to be sized.

Run:  PYTHONPATH=. ./.venv/Scripts/python.exe scripts/check_racing_by_price_band.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import PlacedBet  # noqa: E402

RACING_SPORTS = ("f1", "nascar", "irl")
NOTIONAL = 10.0  # one unit; the point is relative performance per band
BANDS = ((0.0, 0.05), (0.05, 0.15), (0.15, 0.35), (0.35, 0.65), (0.65, 1.01))


def run() -> None:
    db = SessionLocal()
    rows = (
        db.query(PlacedBet)
        .filter(PlacedBet.sport.in_(RACING_SPORTS), PlacedBet.status.in_(("won", "lost")))
        .all()
    )
    rows = [r for r in rows if r.market_prob_at_placement]
    real = sum(1 for r in rows if not r.paper)
    print(f"settled racing bets: {len(rows)}  (real {real}, paper {len(rows) - real})")
    print(f"valued at a notional ${NOTIONAL:.0f} flat stake -- see module docstring\n")

    print(f"{'price band':>12}{'n':>6}{'won':>6}{'hit%':>8}{'implied%':>10}"
          f"{'staked':>10}{'return':>10}{'ROI':>9}")
    for lo, hi in BANDS:
        sub = [r for r in rows if lo <= r.market_prob_at_placement < hi]
        if not sub:
            continue
        won = [r for r in sub if r.status == "won"]
        staked = NOTIONAL * len(sub)
        ret = sum(NOTIONAL / r.market_prob_at_placement for r in won)
        implied = sum(r.market_prob_at_placement for r in sub) / len(sub)
        roi = (ret - staked) / staked
        print(f"{f'{lo:.0%}-{hi:.0%}':>12}{len(sub):>6}{len(won):>6}{len(won) / len(sub):>8.1%}"
              f"{implied:>10.1%}{staked:>10.0f}{ret:>10.0f}{roi:>+9.1%}")

    won_all = [r for r in rows if r.status == "won"]
    staked_all, ret_all = NOTIONAL * len(rows), sum(NOTIONAL / r.market_prob_at_placement for r in won_all)
    print(f"\n{'ALL':>12}{len(rows):>6}{len(won_all):>6}{len(won_all) / len(rows):>8.1%}"
          f"{sum(r.market_prob_at_placement for r in rows) / len(rows):>10.1%}"
          f"{staked_all:>10.0f}{ret_all:>10.0f}{(ret_all - staked_all) / staked_all:>+9.1%}")

    # Calibration is the honest read at this sample size: an ROI on tens of bets
    # is noise, but "did the tail hit as often as its price implies" is a
    # question a small sample can at least gesture at.
    print("\nCALIBRATION (hit rate vs what the MARKET priced -- the baseline that matters):")
    for lo, hi in BANDS:
        sub = [r for r in rows if lo <= r.market_prob_at_placement < hi]
        if len(sub) < 5:
            continue
        hit = sum(1 for r in sub if r.status == "won") / len(sub)
        imp = sum(r.market_prob_at_placement for r in sub) / len(sub)
        exp = imp * len(sub)
        print(f"   {f'{lo:.0%}-{hi:.0%}':>10}  n={len(sub):3d}  market said {imp:.1%} "
              f"(~{exp:.1f} wins)  actual {hit:.1%} ({sum(1 for r in sub if r.status == 'won')} wins)")


if __name__ == "__main__":
    run()
