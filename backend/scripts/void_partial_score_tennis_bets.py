"""Void PAPER tennis derivative bets that were graded off an unfinished match.

WHY. A retirement leaves a partial score ("4-1", "2-6 5-4"). The winner is real
-- both platforms settle the moneyline, and Kalshi's own rule text says so
explicitly ("after a ball has been played") -- but total games, set counts and
margins are not. Until bet_settlement._complete_only was added, every derivative
tennis grader happily read the partial score and returned won/lost.

Same family as the phantom-0.500 and flat-ladder cleanups: the bet was never a
valid observation, so it is voided rather than counted as a loss (a void
contributes 0.0 profit and is excluded from the ROI denominator).

MONEYLINE IS DELIBERATELY LEFT ALONE on these matches -- it is correct.

SAFETY: only touches paper == True. Real bets are reported and skipped.

Dry run by default; pass --apply to write.
"""
import os
import sqlite3
import sys
from collections import defaultdict

DB = os.path.join(os.environ["LOCALAPPDATA"], "nfl-edge-app", "app.db")
APPLY = "--apply" in sys.argv
NOTE = "voided: graded off an unfinished match (retirement/walkover partial score)"

# Everything except moneyline, which grades correctly on a credited winner.
DERIVATIVE_TYPES = ("game_total", "total_sets", "set_winner", "exact_score",
                    "game_spread", "set_spread", "set_total")


def parse_sets(score):
    out = []
    for part in str(score or "").replace(",", " ").split():
        if "-" not in part:
            continue
        try:
            a, b = [int(x) for x in part.split("-")[:2]]
        except ValueError:
            continue
        out.append((a, b))
    return out


def incomplete(score):
    """Mirrors bet_settlement._tennis_match_incomplete -- a finished match needs
    2 won sets, a set being 6+ by two or exactly 7."""
    sets = parse_sets(score)
    if not sets:
        return True
    won = 0
    for a, b in sets:
        hi, lo = max(a, b), min(a, b)
        if (hi >= 6 and hi - lo >= 2) or hi == 7:
            won += 1
    return won < 2


def main():
    c = sqlite3.connect(DB)
    matches = {
        r[0]: r[1] for r in c.execute(
            "SELECT id, score FROM tennis_matches WHERE winner_key IS NOT NULL"
        ).fetchall()
    }
    bad_ids = {mid for mid, score in matches.items() if incomplete(score)}
    print(f"resolved tennis matches: {len(matches)}   unfinished: {len(bad_ids)}")

    rows = c.execute(
        f"SELECT id, market_type, status, paper, tennis_match_id FROM placed_bets "
        f"WHERE sport='tennis' AND status IN ('won','lost') "
        f"AND market_type IN ({','.join('?' * len(DERIVATIVE_TYPES))})",
        DERIVATIVE_TYPES,
    ).fetchall()

    targets, real_hits = [], []
    by_type = defaultdict(int)
    for _id, mtype, status, paper, mid in rows:
        if mid not in bad_ids:
            continue
        if paper:
            targets.append(_id)
            by_type[mtype] += 1
        else:
            real_hits.append((_id, mtype, status))

    print(f"settled DERIVATIVE bets on an unfinished match: {len(targets) + len(real_hits)}")
    print(f"  paper (will void): {len(targets)}   real (left alone): {len(real_hits)}")
    for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"     {t:14} {n}")
    if real_hits:
        print("  REAL bets -- review by hand, not touched:")
        for _id, mtype, status in real_hits:
            print(f"     id={_id} {mtype} currently '{status}'")

    if APPLY and targets:
        c.executemany(
            "UPDATE placed_bets SET status='void', settlement_note=? WHERE id=?",
            [(NOTE, i) for i in targets],
        )
        c.commit()
        print(f"\nAPPLIED: voided {len(targets)} paper bets")
    elif not APPLY:
        print("\n(dry run -- pass --apply to write)")


if __name__ == "__main__":
    main()
