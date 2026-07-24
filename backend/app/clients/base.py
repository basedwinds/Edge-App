import time

import httpx

_client = httpx.Client(timeout=30.0, headers={"User-Agent": "nfl-edge-app/0.1"})


def get_json(url: str, retries: int = 4):
    for attempt in range(retries):
        try:
            resp = _client.get(url)
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError:
            raise
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1 * (attempt + 1))
    raise RuntimeError(f"failed after {retries} retries: {url}")


def paginate(url_builder, list_key: str | None, limit: int = 200, cursor_style: str = "cursor"):
    """Shared pagination helper.

    cursor_style="cursor": Kalshi-style, response has `cursor` field, url_builder(cursor) -> url
    cursor_style="offset": Polymarket-style, url_builder(offset) -> url, response is a bare list
    """
    results = []
    if cursor_style == "cursor":
        cursor = ""
        while True:
            d = get_json(url_builder(cursor))
            items = d.get(list_key, []) if list_key else d
            results.extend(items)
            cursor = d.get("cursor") or ""
            if not cursor or not items:
                break
    else:
        offset = 0
        while True:
            d = get_json(url_builder(offset))
            items = d if list_key is None else d.get(list_key, [])
            if not items:
                break
            results.extend(items)
            offset += limit
            if len(items) < limit:
                break
    return results
