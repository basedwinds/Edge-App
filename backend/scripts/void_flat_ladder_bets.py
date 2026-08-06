"""Void PAPER bets priced off a FLAT total ladder.

WHY. A totals ladder is monotonic by construction: P(over 0.5 goals) >=
P(over 1.5) >= P(over 2.5). If every rung carries the same price, that is not a
market with a tight opinion -- it is a market with NO opinion, and the "price"
is a placeholder. Found 2026-08-06 chasing soccer's implausible +56.6%: 36
game_total bets from 2026-07-23/24 sat on Polymarket ladders quoting 0.52 /
0.495 / 0.505 for over-0.5 / over-1.5 / over-2.5. The model's correct
0.95/0.80/0.57 "beat" that fake 0.50 on every rung and won 83% of the time.

This is the same family as the phantom-0.500 rows handled by
void_phantom_bets.py, but it is NOT caught by that filter: the individual
prices are 0.49-0.52 rather than exactly 0.500. The flat LADDER is what proves
they are junk -- no single price could.

Already fixed upstream: paper_logger._qualifies gained its no-quote/no-volume
gate on 2026-08-04 (it could not exist earlier -- Polymarket bid/ask were
hardcoded None until then). This only cleans rows logged before that.

A void contributes 0.0 profit and is excluded from the ROI denominator, so this
records them as "never a valid observation" rather than as losses.

SAFETY: only ever touches paper == True. Real bets are reported and skipped.

Dry run by default; pass --apply to write.
"""
import os
import sqlite3
import sys
from collections import defaultdict

DB = os.path.join(os.environ["LOCALAPPDATA"], "nfl-edge-app", "app.db")
APPLY = "--apply" in sys.argv

LADDER_TYPES = ("game_total", "total", "team_total")
MIN_RUNGS = 3        # two points can sit anywhere; three flat ones cannot
MAX_SPAN = 0.10      # a real ladder moves more than 10pp end to end
NOTE = "voided: priced off a flat totals ladder (every rung the same, so no real market price)"

MATCH_COLS = (
    "soccer_match_id", "mlb_game_id", "tennis_match_id", "nfl_game_id", "nba_game_id",
    "wnba_game_id", "cfb_game_id", "cs2_match_id", "lol_match_id", "valorant_match_id",
)


def roi(rows):
    """Flat-unit ROI so paper bets with no dollar stake still count."""
    num = den = 0.0
    for status, p in rows:
        if status not in ("won", "lost"):
            continue
        den += 1.0
        num += (1.0 / p - 1.0) if status == "won" else -1.0
    return (100.0 * num / den) if den else None


def main():
    c = sqlite3.connect(DB)
    cols = "id, sport, market_type, status, paper, line, team, source, market_prob_at_placement, " + ", ".join(MATCH_COLS)
    rows = c.execute(
        f"SELECT {cols} FROM placed_bets "
        f"WHERE status IN ('pending','won','lost') AND line IS NOT NULL "
        f"AND market_type IN ({','.join('?' * len(LADDER_TYPES))})",
        LADDER_TYPES,
    ).fetchall()

    ladders = defaultdict(dict)
    meta = {}
    for r in rows:
        _id, sport, mtype, status, paper, line, team, source, p = r[:9]
        if p is None or p <= 0 or p >= 1:
            continue
        match_id = next((v for v in r[9:] if v is not None), None)
        # One ladder = one (sport, market type, match, team, platform). Team is
        # in the key because team_total has a separate ladder per side.
        ladders[(sport, mtype, match_id, team, source)][line] = _id
        meta[_id] = (sport, status, paper, p)

    flat_ids = []
    for key, rungs in ladders.items():
        if len(rungs) < MIN_RUNGS:
            continue
        ps = [meta[i][3] for i in rungs.values()]
        if max(ps) - min(ps) < MAX_SPAN:
            flat_ids.extend(rungs.values())

    targets = [i for i in flat_ids if meta[i][2]]        # paper only
    real_hits = [i for i in flat_ids if not meta[i][2]]

    by_sport = defaultdict(lambda: {"void": [], "keep": []})
    for _id, (sport, status, paper, p) in meta.items():
        if not paper:
            continue
        by_sport[sport]["void" if _id in set(targets) else "keep"].append((status, p))

    print(f"bets sitting on a flat ladder: {len(flat_ids)}  (paper {len(targets)}, real {len(real_hits)})")
    if real_hits:
        print(f"  REAL bets on a flat ladder -- NOT touched, listed for your review: {real_hits}")
    print()
    print(f"{'sport':10} {'voided':>7} {'their ROI':>10} | {'kept':>6} {'ROI kept':>9}")
    for sport, d in sorted(by_sport.items(), key=lambda x: -len(x[1]["void"])):
        if not d["void"]:
            continue
        rv, rk = roi(d["void"]), roi(d["keep"])
        print(
            f"{sport:10} {len(d['void']):7} "
            f"{(f'{rv:+.1f}%' if rv is not None else '-'):>10} | "
            f"{len(d['keep']):6} {(f'{rk:+.1f}%' if rk is not None else '-'):>9}"
        )

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
