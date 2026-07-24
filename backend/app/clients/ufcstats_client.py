"""ufcstats.com client -- free, unauthenticated, but gated behind an
Anubis-style JS proof-of-work challenge (solve a SHA256(nonce:n) with N
leading hex zeros, POST it to /__c, reuse the session cookie for every
subsequent request). Solvable in pure Python, confirmed live 2026-07-17.

This is the only free source of real UFC fight results/fighter data this
app uses -- no paid API, no LLM involved in parsing (plain HTML scraping).

Two important data-quality notes carried over from prior real-data
findings on this exact site (re-verify against any new usage, don't
assume they no longer apply):
  - Fighter bio pages expose career-CUMULATIVE stats (SLpM, TD Avg, etc.)
    frozen as of scrape time, NOT point-in-time-of-fight -- never use
    those fields as a model feature keyed to a specific past fight. Only
    the static physical attributes (height/reach/stance/DOB) are safe to
    use as-is; everything performance-based must be rebuilt point-in-time
    from this app's own scraped fight history (see mma_features.py).
  - Each fight-details page gives an unambiguous per-fighter W/L via
    `fighter_id` (stable URL id), not name or table position -- use that
    id for win/loss, never infer it from column order.
"""
from __future__ import annotations

import hashlib
import re
import time

import httpx
from bs4 import BeautifulSoup

BASE_URL = "http://ufcstats.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
REQUEST_DELAY_SECONDS = 0.35  # polite crawl delay, matches the PoW gate's own implied rate limit


def _fighter_id(href: str) -> str:
    return href.rstrip("/").rsplit("/", 1)[-1]


class UfcStatsClient:
    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "UfcStatsClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _solve_pow(self, html: str) -> tuple[str, int]:
        nonce_match = re.search(r'nonce="([0-9a-f]+)"', html)
        if not nonce_match:
            raise RuntimeError("PoW challenge page had no nonce -- site structure may have changed")
        nonce = nonce_match.group(1)
        zeros_match = re.search(r"new Array\((\d+)\+1\)\.join\('0'\)", html)
        zeros = int(zeros_match.group(1)) if zeros_match else 2
        target = "0" * zeros
        n = 0
        while True:
            digest = hashlib.sha256(f"{nonce}:{n}".encode()).hexdigest()
            if digest.startswith(target):
                return nonce, n
            n += 1

    def get(self, url: str, max_retries: int = 5) -> httpx.Response:
        resp = self._client.get(url)
        for _ in range(max_retries):
            if "Checking your browser" not in resp.text:
                return resp
            nonce, n = self._solve_pow(resp.text)
            self._client.post(f"{BASE_URL}/__c", data={"nonce": nonce, "n": n})
            time.sleep(0.3)
            resp = self._client.get(url)
        raise RuntimeError(f"could not pass PoW challenge for {url} after {max_retries} attempts")

    def list_completed_events(self) -> list[dict]:
        """Paginated -- walks every page of http://ufcstats.com/statistics/events/completed
        until a page returns zero rows. Returns oldest-unknown order (site
        lists newest-first); caller can sort by event_date if needed."""
        events: list[dict] = []
        page = 1
        while True:
            resp = self.get(f"{BASE_URL}/statistics/events/completed?page={page}")
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.select("tr.b-statistics__table-row")
            page_events = []
            for row in rows:
                link = row.select_one("a.b-link")
                date_span = row.select_one("span.b-statistics__date")
                if not link or not link.get("href"):
                    continue
                page_events.append({
                    "event_id": _fighter_id(link["href"]),
                    "event_url": link["href"],
                    "event_name": link.get_text(strip=True),
                    "event_date": date_span.get_text(strip=True) if date_span else None,
                })
            if not page_events:
                break
            events.extend(page_events)
            page += 1
            time.sleep(REQUEST_DELAY_SECONDS)
        # de-dupe (the site's pagination can repeat a boundary row across page loads)
        seen = set()
        deduped = []
        for e in events:
            if e["event_id"] in seen:
                continue
            seen.add(e["event_id"])
            deduped.append(e)
        return deduped

    def list_upcoming_events(self) -> list[dict]:
        """http://ufcstats.com/statistics/events/upcoming -- real scheduled
        cards weeks ahead (confirmed live 2026-07-17: tomorrow's real Du
        Plessis vs. Usman card, matching Kalshi/Polymarket's own listing
        exactly). Not paginated in practice (only ever a handful of rows),
        but walks pages defensively the same way list_completed_events does
        in case that ever changes."""
        events: list[dict] = []
        page = 1
        while True:
            resp = self.get(f"{BASE_URL}/statistics/events/upcoming?page={page}")
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.select("tr.b-statistics__table-row")
            page_events = []
            for row in rows:
                link = row.select_one("a.b-link")
                date_span = row.select_one("span.b-statistics__date")
                if not link or not link.get("href"):
                    continue
                page_events.append({
                    "event_id": _fighter_id(link["href"]),
                    "event_url": link["href"],
                    "event_name": link.get_text(strip=True),
                    "event_date": date_span.get_text(strip=True) if date_span else None,
                })
            if not page_events:
                break
            events.extend(page_events)
            page += 1
            time.sleep(REQUEST_DELAY_SECONDS)
        seen = set()
        deduped = []
        for e in events:
            if e["event_id"] in seen:
                continue
            seen.add(e["event_id"])
            deduped.append(e)
        return deduped

    def get_event_fight_urls(self, event_url: str) -> list[str]:
        resp = self.get(event_url)
        soup = BeautifulSoup(resp.text, "html.parser")
        urls = []
        for row in soup.select("tr.b-fight-details__table-row"):
            link = row.get("data-link")
            if link and link.startswith("http"):
                urls.append(link)
        return urls

    def get_fight_details(self, fight_url: str) -> list[dict] | None:
        """Returns one row per fighter (always 2) or None if the page didn't
        parse as a real completed fight (e.g. a cancelled-bout stub)."""
        resp = self.get(fight_url)
        soup = BeautifulSoup(resp.text, "html.parser")

        persons = soup.select("div.b-fight-details__person")
        if len(persons) != 2:
            return None
        people = []
        for p in persons:
            a = p.select_one("a.b-fight-details__person-link")
            status = p.select_one("i.b-fight-details__person-status")
            if not a or not a.get("href"):
                return None
            people.append({
                "fighter_id": _fighter_id(a["href"]),
                "fighter_url": a["href"],
                "fighter_name": a.get_text(strip=True),
                "result": status.get_text(strip=True) if status else None,  # "W" | "L" | "D" | "NC"
            })

        title_el = soup.select_one("i.b-fight-details__fight-title")
        title = " ".join(title_el.get_text(" ", strip=True).split()) if title_el else None
        is_title_bout = bool(title and "title" in title.lower())
        weight_class = re.sub(r"\s*(Title\s*)?Bout$", "", title or "", flags=re.IGNORECASE).strip() or None

        fields: dict[str, str] = {}
        for item in soup.select("i.b-fight-details__text-item, i.b-fight-details__text-item_first"):
            label = item.select_one("i.b-fight-details__label")
            if not label:
                continue
            key = label.get_text(strip=True).rstrip(":").lower().replace(" ", "_")
            val = item.get_text(" ", strip=True).replace(label.get_text(strip=True), "", 1).strip()
            fields[key] = val

        # Per-fighter "Totals" stat line (KD, sig str, td, sub att, rev, ctrl
        # time) -- career-safe here since these are for THIS single fight,
        # unlike the fighter bio page's cumulative career numbers.
        stats: dict[str, dict[str, str]] = {}
        table = soup.select_one("section.js-fight-section table")
        if table:
            cols = [td.select("p.b-fight-details__table-text") for td in table.select("tbody tr td")]
            if cols:
                name_cells = cols[0]
                fighter_ids_in_order = [
                    _fighter_id(p.select_one("a")["href"])
                    for p in name_cells
                    if p.select_one("a")
                ]
                headers = [th.get_text(strip=True) for th in table.select("thead th")][1:]
                for col_idx, header in enumerate(headers, start=1):
                    if col_idx >= len(cols):
                        continue
                    values = [p.get_text(strip=True) for p in cols[col_idx]]
                    for fid, val in zip(fighter_ids_in_order, values):
                        stats.setdefault(fid, {})[header] = val

        rows = []
        for person in people:
            row = {
                "fight_url": fight_url,
                "fighter_id": person["fighter_id"],
                "fighter_url": person["fighter_url"],
                "fighter_name": person["fighter_name"],
                "result": person["result"],
                "weight_class": weight_class,
                "is_title_bout": is_title_bout,
                "method": fields.get("method"),
                "round": fields.get("round"),
                "time": fields.get("time"),
                "time_format": fields.get("time_format"),
                "referee": fields.get("referee"),
            }
            row.update(stats.get(person["fighter_id"], {}))
            rows.append(row)
        return rows

    def get_fighter_bio(self, fighter_url: str) -> dict | None:
        """Static physical attributes ONLY (height/reach/stance/DOB) -- the
        career-cumulative striking/grappling stats also on this page are
        deliberately NOT returned here, see module docstring."""
        resp = self.get(fighter_url)
        soup = BeautifulSoup(resp.text, "html.parser")
        name_el = soup.select_one("span.b-content__title-highlight")
        if not name_el:
            return None
        bio: dict[str, str | None] = {
            "fighter_id": _fighter_id(fighter_url),
            "fighter_name": name_el.get_text(strip=True),
            "height": None,
            "reach": None,
            "stance": None,
            "dob": None,
        }
        for li in soup.select("li.b-list__box-list-item"):
            text = li.get_text(" ", strip=True)
            if text.startswith("Height:"):
                bio["height"] = text.split(":", 1)[1].strip() or None
            elif text.startswith("Reach:"):
                bio["reach"] = text.split(":", 1)[1].strip() or None
            elif text.startswith("STANCE:"):
                bio["stance"] = text.split(":", 1)[1].strip() or None
            elif text.startswith("DOB:"):
                bio["dob"] = text.split(":", 1)[1].strip() or None
        return bio
