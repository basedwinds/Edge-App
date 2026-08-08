"""Build data/lol_patches.json -- the LoL patch release calendar.

WHY THIS EXISTS. The Valorant patch-signal test reads a hand-assembled
valorant_patches.json scraped from Liquipedia. The equivalent LoL sources are
all gated: leagueoflegends.fandom.com returns 402, and wiki.leagueoflegends.com
sits behind a Cloudflare interstitial that rejects scripted requests (403 with a
"Please wait" challenge page). Individual wiki pages ARE readable one at a time,
but ~87 of them is not a reasonable dependency.

THE SOURCE USED INSTEAD is Riot's own Data Dragon CDN, which is free, public,
requires no key, and is not gated. ddragon publishes the full version list at
/api/versions.json, and every version's asset directory carries a Last-Modified
header from when Riot uploaded it.

WHY Last-Modified IS TRUSTWORTHY HERE -- this was verified, not assumed. Against
three release dates known independently from the official wiki:

    ddragon 14.1.1  Last-Modified 2024-01-09 19:08  vs wiki V14.1  2024-01-10
    ddragon 16.1.1  Last-Modified 2026-01-07 19:07  vs wiki V26.01 2026-01-08
    ddragon 16.15.1 Last-Modified 2026-07-28 19:09  vs wiki V26.15 2026-07-29

Riot stages the CDN exactly one day ahead, at ~19:00 GMT, consistently across
two years and two naming schemes. So release date = Last-Modified date + 1 day.

PRECISION IS +/- 1 DAY and callers should not pretend otherwise. The 2023 entries
were staged at ~20:00 GMT rather than 19:00, so the one-day offset is a strong
regularity rather than a guarantee. That is fine for assigning matches to patch
ERAS -- a boundary off by a day misfiles only matches played within 24h of a
patch -- but it would not support any claim finer than that.

NAMING NOTE: ddragon kept the old numbering when the client moved to year-based
display versions in 2026, so ddragon 16.N is the patch the wiki calls V26.N.
This file records ddragon's numbering; only the dates are used downstream.
"""
from __future__ import annotations

import datetime
import json
import sys
import time
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
OUT = DATA_DIR / "lol_patches.json"
VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
ASSET = "https://ddragon.leagueoflegends.com/cdn/{v}/data/en_US/champion.json"
UA = {"User-Agent": "Mozilla/5.0 (edge-app patch calendar builder)"}

# Seasons that overlap the LoL match cache (2023-01 -> present).
MAJORS = (13, 14, 15, 16)


def get(url: str, method: str = "GET"):
    req = urllib.request.Request(url, headers=UA, method=method)
    return urllib.request.urlopen(req, timeout=30)


def main() -> None:
    versions = json.loads(get(VERSIONS_URL).read().decode("utf-8"))

    # One entry per major.minor -- ddragon ships hotfix builds (13.1.2 etc.)
    # under the same patch, and the patch RELEASE is the lowest build.
    wanted: dict[tuple[int, int], str] = {}
    for v in versions:
        parts = v.split(".")
        if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
            continue
        major, minor, build = int(parts[0]), int(parts[1]), int(parts[2])
        if major not in MAJORS:
            continue
        key = (major, minor)
        if key not in wanted or build < int(wanted[key].split(".")[2]):
            wanted[key] = v

    print(f"{len(wanted)} patches to date, majors {MAJORS}")

    out = []
    for i, (key, v) in enumerate(sorted(wanted.items())):
        try:
            resp = get(ASSET.format(v=v), method="HEAD")
            lm = resp.headers.get("Last-Modified")
        except Exception as exc:  # noqa: BLE001 - one bad version must not kill the run
            print(f"  !! {v}: {exc}")
            continue
        if not lm:
            print(f"  !! {v}: no Last-Modified")
            continue
        staged = datetime.datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z")
        release = (staged + datetime.timedelta(days=1)).date()
        out.append({"patch": f"{key[0]}.{key[1]}", "date": release.isoformat(),
                    "ddragon_version": v, "staged_utc": staged.isoformat()})
        if i % 10 == 0:
            print(f"  {v} staged {staged.date()} -> release {release}")
        time.sleep(0.15)  # be polite to a free CDN

    out.sort(key=lambda p: p["date"])
    if len(out) < 60:
        print(f"REFUSING TO WRITE: only {len(out)} patches resolved, expected ~87")
        sys.exit(1)

    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\nwrote {OUT} -- {len(out)} patches, {out[0]['date']} -> {out[-1]['date']}")

    gaps = [(datetime.date.fromisoformat(b["date"]) - datetime.date.fromisoformat(a["date"])).days
            for a, b in zip(out, out[1:])]
    print(f"gap between patches: median {sorted(gaps)[len(gaps)//2]}d, "
          f"min {min(gaps)}d, max {max(gaps)}d")


if __name__ == "__main__":
    main()
