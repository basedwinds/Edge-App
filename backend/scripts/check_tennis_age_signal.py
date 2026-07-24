"""Checks whether player AGE explains residual variance in the Elo model's
own walk-forward predictions -- same "check first" discipline as MMA's age
adjustment (which WAS real and validated there). Needs birthdates, which
neither tennis-data.co.uk nor tennisexplorer.com's MATCH pages expose --
but tennisexplorer's PLAYER bio pages do ("Age: 39 (22. 5. 1987)",
confirmed live 2026-07-18), and this app already has real player-name ->
tennisexplorer-slug resolution sitting unused in the raw crawl cache (only
Challenger/ITF tier rows were persisted into the merged dataset; the raw
file also has tour-level rows with slugs, covering 93.3% of players with
>=20 matches in the merged dataset via a normalized-name join -- no new
scraping needed for slug resolution, only for the bio pages themselves).

Scoped to players with >=20 matches (6,217 of them resolve to a real slug)
-- covers 81% of all player-match-appearances in the merged dataset, a
representative sample without needing to resolve all 68,628 unique keys
(the vast majority of which are one-off ITF players who wouldn't move the
needle on this check anyway).
"""
import datetime as dt
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from app.ingestion import tennis_data  # noqa: E402
from app.ingestion.tennis_data import normalize_player_key, TENNISEXPLORER_CACHE_PATH  # noqa: E402
from app.models.baseline.elo_tennis import TennisEloState, predict_and_update  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DOB_CACHE_PATH = DATA_DIR / "tennisexplorer_player_dob_cache.json"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_DOB_RE = re.compile(r"Age:\s*\d+\s*\(([\d.\s]+)\)")


def build_key_to_slug_map() -> dict[str, str]:
    raw = json.loads(TENNISEXPLORER_CACHE_PATH.read_text())
    votes: dict[str, Counter] = {}
    for m in raw:
        for name_field, slug_field in [("player_a_name", "player_a_slug"), ("player_b_name", "player_b_slug")]:
            name, slug = m.get(name_field), m.get(slug_field)
            if name and slug:
                key = normalize_player_key(name)
                votes.setdefault(key, Counter())[slug] += 1
    return {k: v.most_common(1)[0][0] for k, v in votes.items()}


def fetch_dob(slug: str) -> str | None:
    try:
        resp = httpx.get(f"https://www.tennisexplorer.com/player/{slug}/", headers={"User-Agent": USER_AGENT}, timeout=15)
        m = _DOB_RE.search(resp.text)
        if not m:
            return None
        day, month, year = [int(x.strip()) for x in m.group(1).split(".") if x.strip()]
        return dt.date(year, month, day).isoformat()
    except Exception:
        return None


def load_or_build_dob_cache(slugs: list[str]) -> dict[str, str]:
    cache: dict[str, str] = json.loads(DOB_CACHE_PATH.read_text()) if DOB_CACHE_PATH.exists() else {}
    missing = [s for s in slugs if s not in cache]
    print(f"{len(cache)} DOBs already cached, {len(missing)} to fetch")
    if missing:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(fetch_dob, s): s for s in missing}
            done = 0
            for fut in as_completed(futures):
                slug = futures[fut]
                dob = fut.result()
                if dob:
                    cache[slug] = dob
                done += 1
                if done % 500 == 0:
                    print(f"  [{done}/{len(missing)}] fetched, {len(cache)} DOBs total so far")
                    DOB_CACHE_PATH.write_text(json.dumps(cache))
        DOB_CACHE_PATH.write_text(json.dumps(cache))
    return cache


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return float("nan")
    return cov / (vx * vy) ** 0.5


def main():
    matches = tennis_data.load_matches()

    key_to_slug = build_key_to_slug_map()
    counts = Counter()
    for m in matches:
        counts[m["player_a_key"]] += 1
        counts[m["player_b_key"]] += 1
    eligible_keys = [k for k, v in counts.items() if v >= 20 and k in key_to_slug]
    print(f"{len(eligible_keys)} players with >=20 matches resolve to a real tennisexplorer slug")

    slugs = sorted({key_to_slug[k] for k in eligible_keys})
    dob_by_slug = load_or_build_dob_cache(slugs)
    dob_by_key = {k: dob_by_slug[key_to_slug[k]] for k in eligible_keys if key_to_slug[k] in dob_by_slug}
    print(f"{len(dob_by_key)} players have a real resolved DOB")

    def age_at(key: str, match_date: str) -> float | None:
        dob = dob_by_key.get(key)
        if not dob:
            return None
        return (dt.date.fromisoformat(match_date) - dt.date.fromisoformat(dob)).days / 365.25

    state = TennisEloState()
    residuals, age_diffs = [], []

    for m in matches:
        p_a = predict_and_update(state, m)
        if m.get("winner_key") is None or m.get("is_retirement") or p_a is None:
            continue
        age_a = age_at(m["player_a_key"], m["match_date"])
        age_b = age_at(m["player_b_key"], m["match_date"])
        if age_a is None or age_b is None:
            continue
        if not (14 <= age_a <= 45 and 14 <= age_b <= 45):  # exclude parsing errors / nonsense ages
            continue
        actual_a = 1.0 if m["winner_key"] == m["player_a_key"] else 0.0
        residuals.append(actual_a - p_a)
        age_diffs.append(age_b - age_a)  # positive = A younger than B

    print(f"Scored matches with both real ages: {len(residuals)}")
    print(f"AGE DIFFERENTIAL (younger - older, positive = player A younger): r={pearson(age_diffs, residuals):.4f}")


if __name__ == "__main__":
    main()
