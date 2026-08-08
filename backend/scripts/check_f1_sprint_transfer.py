"""Does the main-race F1 model transfer to SPRINT races?

THE QUESTION, and why it gates coverage. kalshi_racing_client deliberately holds
back the five sprint series (KXF1RACESPRINT / SPRINTPOLE / SPRINTTOP5 /
SPRINTTOP10 / SPRINTTOPCONSTRUCTOR) with this reasoning: settlement needs a
sprint-specific results source, and "the main race Elo must NOT be assumed to
transfer -- a sprint is a third the distance with no mandatory stop and a
different overtaking profile. That is a model question, not coverage."

The settlement half is now answered: ESPN's f1 feed exposes each weekend's
sessions separately, with a completed "SR" (sprint race) competition carrying a
full 20-car finishing order. So sprints ARE settleable.

This answers the model half. If driver ratings built ONLY from grands prix
predict sprint finishing order about as well as they predict grand prix
finishing order, the existing model transfers and the sprint markets are
coverage, not modelling. If sprints are materially harder to predict, they need
their own model and should stay unpriced.

METHOD. Walk forward through grands prix building the same pairwise Elo
production uses (racing_ratings K/regression), and at each event predict a
finishing ORDER purely from ratings. Score two things per session type:

  rank correlation  -- does the rating order match the finishing order?
  winner hit        -- how often is the top-rated driver the winner?

Grands prix are the control. A sprint result close to the GP result means
transfer; materially worse means it does not.

DELIBERATELY NO GRID TERM here. Sprint grids come from a separate sprint
qualifying session and grand prix grids from Saturday qualifying, so including
either would compare two different information sets and confound the answer.
This isolates the question actually being asked: does DRIVER STRENGTH carry
across formats?

SAMPLE IS SMALL AND THAT IS STATED UP FRONT: ESPN exposes 22 completed sprints
(2023-2026; 2021-22 sprints are not typed as SR in its feed). ~440 driver-
sprints against ~2,400 driver-grands-prix. Enough to detect a large difference,
not enough to certify a small one.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.models.baseline.racing_ratings import BASE, K_DRIVER, SEASON_REGRESSION, _pairwise

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/racing/f1/scoreboard"
WARMUP = 15


def fetch_sprints() -> list[dict]:
    """[{date, name, order: {driver_name: finish_pos}}] for every completed
    sprint race ESPN exposes."""
    c = httpx.Client(timeout=45, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
    out = []
    for yr in range(2021, 2027):
        try:
            evs = c.get(ESPN, params={"dates": f"{yr}0101-{yr}1231", "limit": "100"}).json().get("events") or []
        except Exception:
            continue
        for e in evs:
            for comp in e.get("competitions") or []:
                if (comp.get("type") or {}).get("abbreviation") != "SR":
                    continue
                if ((comp.get("status") or {}).get("type") or {}).get("state") != "post":
                    continue
                order = {}
                for k in comp.get("competitors") or []:
                    nm = ((k.get("athlete") or {}).get("displayName") or "").strip()
                    try:
                        pos = int(k.get("order"))
                    except (TypeError, ValueError):
                        continue
                    if nm:
                        order[nm] = pos
                if len(order) >= 8:
                    out.append({"date": (e.get("date") or "")[:10], "name": e.get("name"), "order": order})
    out.sort(key=lambda r: r["date"])
    return out


def rank_corr(pairs) -> float | None:
    """Pearson on ranks -- Spearman without a scipy dependency."""
    if len(pairs) < 5:
        return None
    xs = [a for a, _ in pairs]
    ys = [b for _, b in pairs]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in pairs)
    den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
    return num / den if den else None


def main() -> None:
    races = list(json.loads((DATA_DIR / "racing_f1.json").read_text(encoding="utf-8")).values())
    races.sort(key=lambda r: (r.get("date") or "", r["id"]))
    sprints = fetch_sprints()
    print(f"grands prix in cache: {len(races)}   completed sprints from ESPN: {len(sprints)}")
    if not sprints:
        print("no sprint data -- cannot answer")
        return

    by_date = defaultdict(list)
    for s in sprints:
        by_date[s["date"]].append(s)

    drv: dict[str, float] = {}
    name_of: dict[str, str] = {}
    cur_season = None
    gp_pairs, sp_pairs = [], []
    gp_hit = gp_n = sp_hit = sp_n = 0
    sprints_scored = 0

    for i, race in enumerate(races):
        if race.get("season") != cur_season:
            cur_season = race.get("season")
            for d in drv:
                drv[d] = BASE + (1 - SEASON_REGRESSION) * (drv[d] - BASE)

        results = race["results"]
        rdate = (race.get("date") or "")[:10]

        if i >= WARMUP:
            # --- score the GRAND PRIX (control) ---------------------------
            rated = [(r["driver_id"], r["order"]) for r in results if r["driver_id"] in drv]
            if len(rated) >= 8:
                ranked = sorted(rated, key=lambda x: -drv[x[0]])
                gp_pairs += [(pos + 1, actual) for pos, (_, actual) in enumerate(ranked)]
                gp_n += 1
                top = ranked[0][0]
                gp_hit += int(any(r["driver_id"] == top and r.get("winner") for r in results))

            # --- score any SPRINT on this weekend, same ratings ------------
            # Matched by DATE WINDOW: a sprint runs the day before its grand
            # prix, so anything in the two days before this race belongs to
            # this weekend and is priced off ratings that have not yet seen it.
            for s in sprints:
                if not rdate or not s["date"]:
                    continue
                delta = (
                    __import__("datetime").date.fromisoformat(rdate)
                    - __import__("datetime").date.fromisoformat(s["date"])
                ).days
                if not (0 <= delta <= 2):
                    continue
                pairs = []
                for r in results:
                    nm = r.get("driver")
                    did = r["driver_id"]
                    if nm and did in drv and nm in s["order"]:
                        pairs.append((did, s["order"][nm]))
                if len(pairs) >= 8:
                    ranked = sorted(pairs, key=lambda x: -drv[x[0]])
                    sp_pairs += [(pos + 1, actual) for pos, (_, actual) in enumerate(ranked)]
                    sp_n += 1
                    sprints_scored += 1
                    sp_hit += int(ranked[0][1] == 1)

        # ---- update ratings with the GRAND PRIX result ---------------------
        field = [r["driver_id"] for r in results]
        order = {r["driver_id"]: r["order"] for r in results}
        d_rat = {d: drv.get(d, BASE) for d in field}
        drv.update(_pairwise(field, order, d_rat, K_DRIVER))
        for r in results:
            name_of[r["driver_id"]] = r.get("driver")

    gc, sc = rank_corr(gp_pairs), rank_corr(sp_pairs)
    print(f"\nscored {gp_n} grands prix and {sprints_scored} sprints "
          f"({len(gp_pairs)} vs {len(sp_pairs)} driver-results)")
    print(f"\n{'session':16s} {'rank corr':>10s} {'winner hit':>11s} {'events':>7s}")
    print(f"{'grand prix':16s} {gc if gc is not None else float('nan'):10.3f} "
          f"{gp_hit/gp_n if gp_n else 0:10.1%} {gp_n:7d}")
    print(f"{'sprint':16s} {sc if sc is not None else float('nan'):10.3f} "
          f"{sp_hit/sp_n if sp_n else 0:10.1%} {sp_n:7d}")
    if gc and sc:
        print(f"\nsprint rank-correlation is {sc/gc*100:.0f}% of the grand prix figure")
        print("VERDICT:", "TRANSFERS -- sprints are coverage, not a new model"
              if sc >= gc * 0.85 else
              "DOES NOT TRANSFER -- sprints need their own model; keep them unpriced")


if __name__ == "__main__":
    main()
