"""Throwaway experiment (not wired into production): tests whether a
PLAYER-level rating beats the shipped TEAM-name Elo for Valorant, mirroring
the CS2 pilot (scripts/test_cs2_player_level_signal.py) that closed ~51% of
the real market gap.

Data-quality difference worth stating: CS2's lineups are approximated from
per-EVENT participant rosters, while these are vlr.gg's real PER-MATCH
scoreboards -- the exact 5 who actually played that match. CS2's own results
showed extra COVERAGE hit sharp diminishing returns while quality was the
open lever, which is the whole reason Valorant was done second.

Team-side training mirrors the shipped Valorant rule exactly (PER-MAP
updates off the real maps_won_a/maps_won_b split, K=36 -- see
elo_valorant.py::update_ratings), so the baseline is the real current model,
not a strawman. Player ratings update once per real SERIES (same granularity
the h2h signal was validated at for this title).

Lineups are joined to team_a/team_b by NAME (this app's own
market_matcher_valorant.team_names_match), never by vlr.gg's row order --
the crawl confirmed real naming variants like
"JD Mall JDG Esports(JDG Esports)" vs this app's "JDG Esports".
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion.market_matcher_valorant import team_names_match  # noqa: E402
from app.ingestion.valorant_data import infer_best_of_from_score  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo_valorant import (  # noqa: E402
    BASE_RATING, K, RATING_CLAMP, map_win_prob, series_score_distribution,
)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "valorant_historical_match_cache.json"
LINEUP_CACHE_PATH = DATA_DIR / "valorant_match_lineups_cache.json"
WARMUP = 500


def load_matches():
    rows = json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8"))
    rows = [r for r in rows if r["match_date"] >= "2020-01-01"]
    for r in rows:
        if not r.get("best_of"):
            r["best_of"] = infer_best_of_from_score(r.get("maps_won_a"), r.get("maps_won_b"))
    rows = [r for r in rows if r.get("best_of") and r.get("winner")]
    rows.sort(key=lambda r: r.get("estimated_start_time") or r["match_date"])
    return rows


def load_lineups():
    if not LINEUP_CACHE_PATH.exists():
        return {}
    return {k: v for k, v in json.loads(LINEUP_CACHE_PATH.read_text(encoding="utf-8")).items() if v}


def _name_variants(name: str) -> list[str]:
    """vlr.gg renders SPONSORED team names with the canonical name in
    parentheses -- confirmed live on the real crawl: "JD Mall JDG
    Esports(JDG Esports)", "Movistar KOI(KOI)", "Guangzhou Huadu Bilibili
    Gaming(Bilibili Gaming)", "VISA KRU(KRU Esports)". This app's own match
    cache stores the canonical form, so the parenthetical has to be tried as
    well or every sponsored team silently drops out of the join."""
    variants = [name]
    if "(" in name and name.rstrip().endswith(")"):
        inner = name[name.rfind("(") + 1:-1].strip()
        if inner:
            variants.append(inner)
    return variants


def _same_team(a: str, b: str) -> bool:
    return any(team_names_match(x, y) for x in _name_variants(a) for y in _name_variants(b))


def lineups_for(entry, team_a, team_b):
    """Returns (lineup_a, lineup_b) oriented onto THIS match's team_a/team_b,
    or (None, None) if the scraped names can't be confidently matched. Never
    falls back to positional order -- a silently flipped lineup would poison
    real player ratings in both directions at once."""
    if not entry:
        return None, None
    names = entry["teams"]
    lus = entry["lineups"]
    if _same_team(names[0], team_a) and _same_team(names[1], team_b):
        return lus[0], lus[1]
    if _same_team(names[0], team_b) and _same_team(names[1], team_a):
        return lus[1], lus[0]
    return None, None


def series_prob(map_p, best_of):
    dist = series_score_distribution(map_p, best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def run(matches, lineup_cache, k_player):
    team_r, player_r = {}, {}
    team_preds, player_preds, outcomes = [], [], []

    def apply_map(ta, tb, actual_a):
        ar, br = team_r.get(ta, BASE_RATING), team_r.get(tb, BASE_RATING)
        d = K * (actual_a - map_win_prob(ar, br))
        team_r[ta] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, ar + d))
        team_r[tb] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, br - d))

    for idx, m in enumerate(matches):
        ta, tb, bo, w = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        ma, mb = m.get("maps_won_a"), m.get("maps_won_b")
        la, lb = lineups_for(lineup_cache.get(str(m["source_match_id"])), ta, tb)
        ar, br = team_r.get(ta, BASE_RATING), team_r.get(tb, BASE_RATING)
        actual = 1.0 if w == "team_a" else 0.0

        if la and lb and idx >= WARMUP:
            team_preds.append(series_prob(map_win_prob(ar, br), bo))
            a_str = sum(player_r.get(p, BASE_RATING) for p in la) / len(la)
            b_str = sum(player_r.get(p, BASE_RATING) for p in lb) / len(lb)
            player_preds.append(series_prob(map_win_prob(a_str, b_str), bo))
            outcomes.append(actual)

        # team: shipped PER-MAP rule
        if ma is not None and mb is not None and (ma + mb) > 0:
            for _ in range(ma):
                apply_map(ta, tb, 1.0)
            for _ in range(mb):
                apply_map(ta, tb, 0.0)
        else:
            apply_map(ta, tb, actual)

        # player: once per real series
        if la and lb:
            a_str = sum(player_r.get(p, BASE_RATING) for p in la) / len(la)
            b_str = sum(player_r.get(p, BASE_RATING) for p in lb) / len(lb)
            d = k_player * (actual - map_win_prob(a_str, b_str))
            for p in la:
                player_r[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, player_r.get(p, BASE_RATING) + d))
            for p in lb:
                player_r[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, player_r.get(p, BASE_RATING) - d))

    return team_preds, player_preds, outcomes, player_r


def main():
    matches = load_matches()
    lineup_cache = load_lineups()
    joined = sum(1 for m in matches if all(lineups_for(lineup_cache.get(str(m["source_match_id"])), m["team_a"], m["team_b"])))
    print(f"{len(matches)} matches | {len(lineup_cache)} scraped lineups | {joined} joined to team_a/team_b by name")

    tp, pp, out, pr = run(matches, lineup_cache, k_player=24.0)
    if not out:
        print("no evaluable matches yet -- crawl still in progress?")
        return
    base = brier_score(tp, out)
    print(f"\n{len(out)} evaluated matches | {len(pr)} players rated")
    print(f"  TEAM model Brier: {base:.5f}\n")
    print(f"{'k_player':>9}  {'pure player':>12}  {'best w':>7}  {'blend':>10}  {'vs team':>10}")
    for k_p in (8, 12, 16, 24, 32, 40):
        tp2, pp2, out2, _ = run(matches, lineup_cache, k_player=k_p)
        pure = brier_score(pp2, out2)
        best_w, best_b = None, None
        for w in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
            bb = brier_score([(1 - w) * a + w * b for a, b in zip(tp2, pp2)], out2)
            if best_b is None or bb < best_b:
                best_b, best_w = bb, w
        print(f"{k_p:>9}  {pure:>12.5f}  {best_w:>7}  {best_b:>10.5f}  {best_b - base:>+10.5f}")


if __name__ == "__main__":
    main()
