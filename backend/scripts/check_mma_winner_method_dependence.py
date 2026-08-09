"""Is METHOD independent of WHO WINS? (Task #109.)

WHY THIS HAS TO BE MEASURED BEFORE method_of_victory CAN BE PRICED. That market
asks a JOINT question -- "does fighter A win, AND by KO" -- while this app owns
two separate MARGINAL models: elo_service_mma gives P(A wins), and
method_service_mma gives P(method = m) for the fight as a whole. The tempting
move is to multiply them:

    P(A wins by KO)  =?=  P(A wins) x P(KO)

That is only correct if method is independent of the winner's identity, and
there is an obvious reason to doubt it: a dominant favourite plausibly finishes
opponents more often than an underdog who scrapes a win does. If that holds, the
product systematically UNDERSTATES the favourite's finish markets and OVERSTATES
the underdog's -- on every fight, in the same direction, which is exactly the
kind of bias that looks like a stable edge and is really a modelling error.

THE TEST. Replay every UFC fight in chronological order, carrying a walk-forward
Elo so each fight is judged on ratings that existed BEFORE it happened (a
present-day rating would leak the result being predicted). Then split the
finished fights by whether the PRE-FIGHT FAVOURITE won, and compare the method
distribution of the two groups. Under independence they should match.

Also reported: the same split by how big the rating gap was, because "the
favourite finishes more" and "a MISMATCH finishes more" are different claims and
only the first one breaks the product rule. A gap effect that applies equally to
both fighters is already captured by the marginal method model.

Draws, no-contests, DQs and overturned results are excluded -- they have no
winner, so they cannot speak to a winner-conditional question.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CACHE = Path(__file__).resolve().parents[2] / "data" / "ufc_fight_cache.json"
K = 24.0          # Elo step; only the ORDERING of ratings matters here
BASE = 1500.0
MIN_PRIOR = 3     # fights each side must already have before a bout counts


def method_bucket(method: str | None) -> str | None:
    """KO / SUB / DEC, or None for anything without a real winner."""
    if not method:
        return None
    m = method.lower()
    if "ko/tko" in m or "doctor" in m:
        return "KO/TKO"
    if "submission" in m:
        return "Submission"
    if "decision" in m:
        return "Decision"
    return None  # DQ, Overturned, Could Not Continue, Other


def parse_date(s: str | None):
    for fmt in ("%B %d, %Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def load_fights():
    """Pair the per-fighter rows into fights, chronologically.

    The cache stores ONE ROW PER FIGHTER PER FIGHT, sharing a fight_url. `result`
    is W/L/D/NC -- not "win"/"loss", a distinction that has already cost this
    project one wrong answer."""
    rows = json.loads(CACHE.read_text(encoding="utf-8"))
    by_fight: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        key = r.get("fight_url") or f"{r.get('event_id')}|{r.get('method')}|{r.get('round')}"
        by_fight[key].append(r)

    fights = []
    for key, pair in by_fight.items():
        if len(pair) != 2:
            continue
        a, b = pair
        date = parse_date(a.get("event_date"))
        if date is None:
            continue
        res_a = (a.get("result") or "").strip().upper()
        res_b = (b.get("result") or "").strip().upper()
        if {res_a, res_b} != {"W", "L"}:
            continue  # draw / NC / missing -- no winner to condition on
        winner, loser = (a, b) if res_a == "W" else (b, a)
        bucket = method_bucket(a.get("method"))
        if bucket is None:
            continue
        fights.append({
            "date": date, "winner": winner.get("fighter_id"), "loser": loser.get("fighter_id"),
            "bucket": bucket, "title": bool(a.get("is_title_bout")),
        })
    fights.sort(key=lambda f: f["date"])
    return fights


def main() -> None:
    fights = load_fights()
    print(f"{len(fights)} decided UFC fights with a usable method\n")
    if len(fights) < 500:
        print("TOO FEW -- stopping.")
        return

    rating: dict[str, float] = {}
    seen: collections.Counter = collections.Counter()
    graded = []  # (favourite_won, bucket, gap)

    for f in fights:
        w, l = f["winner"], f["loser"]
        rw, rl = rating.get(w, BASE), rating.get(l, BASE)
        if seen[w] >= MIN_PRIOR and seen[l] >= MIN_PRIOR and rw != rl:
            graded.append((rw > rl, f["bucket"], abs(rw - rl)))
        # walk forward AFTER grading, so the fight never informs its own prediction
        exp_w = 1.0 / (1.0 + 10 ** ((rl - rw) / 400.0))
        rating[w] = rw + K * (1.0 - exp_w)
        rating[l] = rl - K * (1.0 - exp_w)
        seen[w] += 1
        seen[l] += 1

    print(f"{len(graded)} fights where both had {MIN_PRIOR}+ prior bouts\n")
    buckets = ("KO/TKO", "Submission", "Decision")

    fav = [b for won, b, _g in graded if won]
    dog = [b for won, b, _g in graded if not won]
    print("METHOD BY WHO WON  (independence => these two columns match)")
    print(f"{'method':14s}{'favourite won':>16s}{'underdog won':>15s}{'diff':>9s}")
    for bkt in buckets:
        pf = fav.count(bkt) / len(fav)
        pd_ = dog.count(bkt) / len(dog)
        print(f"{bkt:14s}{pf:16.3f}{pd_:15.3f}{pf - pd_:+9.3f}")
    print(f"{'n':14s}{len(fav):16d}{len(dog):15d}")

    # Chi-square on the 2x3 table -- is the difference bigger than noise?
    chi = 0.0
    tot = len(fav) + len(dog)
    for bkt in buckets:
        obs_f, obs_d = fav.count(bkt), dog.count(bkt)
        exp_row = (obs_f + obs_d)
        for obs, n_grp in ((obs_f, len(fav)), (obs_d, len(dog))):
            exp = exp_row * n_grp / tot
            if exp > 0:
                chi += (obs - exp) ** 2 / exp
    print(f"\nchi-square (2x3, 2 df) = {chi:.1f}   "
          f"[9.2 = p<0.01, 13.8 = p<0.001]")

    # Does the effect track the rating GAP? A gap effect is symmetric and is
    # already inside the marginal method model; a winner effect is not.
    print("\nSAME SPLIT BY RATING GAP")
    gaps = sorted(g for _w, _b, g in graded)
    q1, q3 = gaps[len(gaps) // 4], gaps[3 * len(gaps) // 4]
    for label, lo, hi in (("close   (<Q1)", 0, q1), ("mid", q1, q3), ("mismatch (>Q3)", q3, 10 ** 9)):
        sub = [(w, b) for w, b, g in graded if lo <= g < hi]
        if not sub:
            continue
        sf = [b for w, b in sub if w]
        sd = [b for w, b in sub if not w]
        if not sf or not sd:
            continue
        fin_f = sum(1 for b in sf if b != "Decision") / len(sf)
        fin_d = sum(1 for b in sd if b != "Decision") / len(sd)
        print(f"  {label:16s} n={len(sub):5d}  P(finish | fav won) {fin_f:.3f}   "
              f"P(finish | dog won) {fin_d:.3f}   diff {fin_f - fin_d:+.3f}")

    # What the naive product would cost, in the units that matter: a price.
    p_fin_overall = sum(1 for _w, b, _g in graded if b != "Decision") / len(graded)
    p_fin_fav = sum(1 for b in fav if b != "Decision") / len(fav)
    print(f"\nP(finish) overall {p_fin_overall:.3f} vs P(finish | favourite won) "
          f"{p_fin_fav:.3f}  -> a product using the marginal is off by "
          f"{abs(p_fin_fav - p_fin_overall) * 100:.1f}pp on the favourite's finish markets")


    # ---- DOES CORRECTING FOR IT PREDICT BETTER OUT OF SAMPLE? -------------
    # A statistically real dependence is not automatically a useful one. Fit the
    # winner-conditional multipliers on the FIRST 70% of fights chronologically
    # and score both schemes on the last 30%, by log-loss over the 3-way method
    # outcome given who actually won.
    #
    #   naive     : P(method) from the training marginal, ignoring the winner
    #   corrected : that marginal reweighted by whether the FAVOURITE won
    #
    # Chronological, not random: a random split would let a fighter's later
    # fights inform his earlier ones.
    split = int(len(graded) * 0.7)
    train, test = graded[:split], graded[split:]
    if len(test) < 300:
        print("\nheld-out set too small to validate")
        return

    def dist(rows):
        n = max(1, len(rows))
        return {b: max(1e-6, sum(1 for x in rows if x == b) / n) for b in buckets}

    marg = dist([b for _w, b, _g in train])
    fav_d = dist([b for w, b, _g in train if w])
    dog_d = dist([b for w, b, _g in train if not w])

    ll_naive = ll_corr = 0.0
    for won, b, _g in test:
        ll_naive -= math.log(marg[b])
        ll_corr -= math.log((fav_d if won else dog_d)[b])
    n = len(test)
    print(f"\nHELD-OUT VALIDATION  (train {len(train)} -> test {n}, chronological)")
    print(f"  naive marginal      log-loss {ll_naive / n:.4f}")
    print(f"  winner-conditional  log-loss {ll_corr / n:.4f}")
    print(f"  improvement         {(ll_naive - ll_corr) / n:+.4f} nats/fight "
          f"({'BETTER' if ll_corr < ll_naive else 'WORSE'})")
    print("\n  training multipliers vs the marginal (what the correction does):")
    for b in buckets:
        print(f"    {b:12s} favourite x{fav_d[b] / marg[b]:.3f}   underdog x{dog_d[b] / marg[b]:.3f}")


if __name__ == "__main__":
    main()
