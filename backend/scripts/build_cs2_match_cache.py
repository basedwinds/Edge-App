"""One-off local cache builder for CS2 historical match results, scraped
fresh from liquipedia.net -- parallel to build_ufc_fight_cache.py, but a
genuinely different crawl shape: there's no single "list every event" index
page the way ufcstats.com has. Instead this crawls S-Tier/A-Tier
Tournaments (the two curated tier-list pages, confirmed live 2026-07-19 to
be organized in one <table> per YEAR with real tournament page links +
date ranges), keeps only tournaments whose date range has already
concluded, discovers each tournament's real subpages via the MediaWiki
`action=query&list=allpages&apprefix=<tournament>/` API (confirmed live:
this reliably found "StarLadder/2025/Major/Stage 1"|"Stage 2"|"Stage 3"|
"Playoffs" without needing to guess a naming convention), then extracts
every real match from each (sub)page via
app.ingestion.cs2_data.parse_matches_from_html -- the SAME generic parser
the live poller uses, since a tournament's bracket/matchlist popups use the
exact same internal match-info-header/match-info-countdown DOM shape as the
live Liquipedia:Matches page (confirmed live, see that function's own
docstring).

Deliberately S-Tier only for this first pass (not A-Tier/B-Tier) -- S-Tier
tournaments are the ones whose teams overlap most heavily with what Kalshi
actually trades (confirmed live: FlyQuest, Astralis, MOUZ, Imperial, NAVI-
tier orgs all appear in both), so this maximizes real training signal per
tournament crawled rather than crawling thousands of small regional/
qualifier events for comparatively little benefit. A-Tier is a reasonable
future extension, not attempted here.

Checkpoints to disk after every tournament (same resume-on-interrupt
discipline as build_ufc_fight_cache.py) -- re-running resumes from the last
completed tournament rather than re-crawling from scratch.

Run: backend/.venv/Scripts/python.exe scripts/build_cs2_match_cache.py
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from app.ingestion.cs2_data import parse_matches_from_html  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "cs2_historical_match_cache.json"
PROGRESS_PATH = DATA_DIR / "cs2_scrape_progress.json"

API_URL = "https://liquipedia.net/counterstrike/api.php"
BASE_URL = "https://liquipedia.net/counterstrike"
TIER_PAGES = ["S-Tier_Tournaments", "A-Tier_Tournaments"]  # extended 2026-07-20 to grow the real market-odds backtest sample (see backtest_cs2_market_odds.py) -- more tournaments = more matched Kalshi settled markets
REQUEST_DELAY_SECONDS = 0.75  # polite crawl delay -- Liquipedia's own API terms warn calls are rate limited

_client = httpx.Client(
    timeout=30.0,
    headers={"User-Agent": "nfl-edge-app/0.1 (personal research project; contact via GitHub)"},
)

_MONTH_ABBR = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
)}


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=None), encoding="utf-8")


def _parse_end_date(date_range_text: str, year_heading: str) -> "dt_date | None":
    """"Nov 30 - Dec 15, 2024" or "Oct 07-13, 2024" -> the END date, so we can
    filter to only-already-concluded tournaments. Liquipedia renders the
    separator as a non-ascii en-dash (mojibake-prone depending on encoding),
    matched structurally below rather than via a literal dash character.

    REAL BUG this fixes (caught live 2026-07-19, not assumed): a same-month
    range like "Oct 07-13, 2024" has no second month abbreviation before the
    end day -- an earlier version of this regex REQUIRED one whenever the
    optional end-month/end-day group matched at all, so the whole match
    failed outright (not just a bad fallback) for every same-month
    tournament, silently dropping the majority of real concluded
    tournaments from the 2024 table (18 real rows, most single-month
    ranges) from this filter entirely. Rewritten to find the END DAY as
    whatever digit run sits right before ", YEAR", then separately check
    for a month abbreviation immediately preceding THAT day -- present for
    a cross-month range, absent (reuses the string's own first/start month)
    for a same-month range."""
    import datetime as real_dt
    m_year = re.search(r",\s*(\d{4})\s*$", date_range_text)
    if not m_year:
        return None
    year = m_year.group(1)
    before_year = date_range_text[: m_year.start()]

    m_end_day = re.search(r"(\d{1,2})\s*$", before_year)
    if not m_end_day:
        return None
    end_day = m_end_day.group(1)
    before_day = before_year[: m_end_day.start()]

    m_end_month = re.search(r"([A-Za-z]{3})\s*$", before_day)
    if m_end_month:
        end_mon = m_end_month.group(1)
    else:
        m_start_month = re.match(r"\s*([A-Za-z]{3})", date_range_text)
        end_mon = m_start_month.group(1) if m_start_month else None

    month_num = _MONTH_ABBR.get(end_mon) if end_mon else None
    if month_num is None:
        return None
    try:
        return real_dt.date(int(year), month_num, int(end_day))
    except ValueError:
        return None


def list_concluded_tournaments() -> list[dict]:
    """Returns [{name, slug, end_date_iso}] for every S-Tier tournament whose
    own listed date range has already concluded (real end date < today),
    across every year table on the tier page (confirmed live: one <table>
    per year heading, e.g. "2027"/"2026"/"2025"/"2024")."""
    import datetime as real_dt
    today = real_dt.date.today()
    tournaments = []
    for tier_page in TIER_PAGES:
        resp = _client.get(f"{BASE_URL}/{tier_page}")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for table in soup.find_all("table", class_="table2__table"):
            for row in table.find_all("tr", class_=lambda c: c and "table2__row--body" in c):
                name_td = row.find("td", class_="column__tournament")
                if name_td is None:
                    continue
                link = name_td.find("a")
                if link is None:
                    continue
                tds = row.find_all("td")
                date_td = tds[3] if len(tds) > 3 else None
                date_text = date_td.get_text(strip=True) if date_td is not None else ""
                end_date = _parse_end_date(date_text, "")
                if end_date is None or end_date >= today:
                    continue  # unparseable or not yet concluded -- skip rather than guess
                href = link.get("href") or ""
                slug = href.lstrip("/").removeprefix("counterstrike/")
                tournaments.append({"name": link.get_text(strip=True), "slug": slug, "end_date_iso": end_date.isoformat()})
    return tournaments


def list_subpages(slug: str) -> list[str]:
    """Real subpages via the MediaWiki API (see module docstring) -- returns
    [] if the tournament has none (small tournaments sometimes keep
    everything on the main page)."""
    resp = _client.get(API_URL, params={
        "action": "query", "list": "allpages", "apprefix": f"{slug}/", "aplimit": 50, "format": "json",
    })
    resp.raise_for_status()
    data = resp.json()
    return [p["title"] for p in data.get("query", {}).get("allpages", [])]


def crawl_tournament(name: str, slug: str) -> list[dict]:
    pages_to_fetch = [slug] + list_subpages(slug)
    time.sleep(REQUEST_DELAY_SECONDS)
    matches: dict[str, dict] = {}
    for page_title in pages_to_fetch:
        try:
            resp = _client.get(f"{BASE_URL}/{page_title.replace(' ', '_')}")
            resp.raise_for_status()
        except httpx.HTTPError:
            continue
        for row in parse_matches_from_html(resp.text, default_event_name=name, default_tournament_slug=slug):
            matches[row["source_match_id"]] = row
        time.sleep(REQUEST_DELAY_SECONDS)
    return list(matches.values())


def main():
    tournaments = load_json(DATA_DIR / "cs2_tournament_list_cache.json", None)
    if tournaments is None:
        print("Listing concluded S-Tier tournaments...")
        tournaments = list_concluded_tournaments()
        print(f"  {len(tournaments)} concluded tournaments found")
        save_json(DATA_DIR / "cs2_tournament_list_cache.json", tournaments)
    else:
        print(f"Loaded {len(tournaments)} tournaments from cache")

    progress = load_json(PROGRESS_PATH, {"done_slugs": []})
    done_slugs = set(progress["done_slugs"])
    all_matches: dict[str, dict] = {m["source_match_id"]: m for m in load_json(MATCH_CACHE_PATH, [])}

    remaining = [t for t in tournaments if t["slug"] not in done_slugs]
    print(f"{len(done_slugs)} tournaments already scraped, {len(remaining)} remaining")

    for i, t in enumerate(remaining):
        print(f"[{i + 1}/{len(remaining)}] {t['name']} ({t['slug']})...", end=" ", flush=True)
        try:
            matches = crawl_tournament(t["name"], t["slug"])
        except httpx.HTTPError as e:
            print(f"FAILED ({e}), will retry on next run")
            continue
        for m in matches:
            all_matches[m["source_match_id"]] = m
        print(f"{len(matches)} matches")

        done_slugs.add(t["slug"])
        progress["done_slugs"] = sorted(done_slugs)
        save_json(PROGRESS_PATH, progress)
        save_json(MATCH_CACHE_PATH, list(all_matches.values()))

    print(f"\nDone. {len(all_matches)} total real historical matches cached at {MATCH_CACHE_PATH}")


if __name__ == "__main__":
    main()
