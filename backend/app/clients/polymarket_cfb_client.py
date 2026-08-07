"""Polymarket College Football futures.

CFB was the ONLY sport in this app with zero Polymarket coverage -- confirmed by
a full audit on 2026-08-07 across every sport and league we carry (CBB returns 0
events under cbb/college-basketball/ncaab/march-madness, Eredivisie 0 under four
slug variants, and the `mma` tag is 24/25 duplicate of `ufc`, so those are real
absences of supply, not gaps). CFB had real supply and no pipeline.

FOUR TAGS, NOT ONE. `cfb` (26 events) and `ncaaf` (24) overlap heavily but
neither is a superset, and `college-football` / `ncaa-football` each carry an
event the other two miss -- including the single most-traded one. Deduped by
event slug, same multi-tag shape as polymarket_cs2_client.

WHAT IS DELIBERATELY NOT INGESTED, and why -- so an audit doesn't rediscover it:
  - Heisman (60), Coach of the Year (42), Rushing Yards Leader (44), Top Ranked
    Offense/Defense (67), Cover Athlete (16): player/award/novelty. No model, and
    the player-stat family is already measured and disabled elsewhere.
  - "Team to be Ranked #1" (17): resolves on a human POLL, not on results. The
    season sim cannot answer it even in principle.
  - "Conference to Win National Championship" / "Conference of Heisman Winner":
    the subject is a conference, not a team. The sim does expose
    title_by_conference, but that market's field is the 10+ FBS conferences
    while Kalshi's cfb_title_conference is 4 named conferences plus OTHER, so
    the two are not the same field and would need their own mapping.
  - "Team to have an Undefeated Season" (27, $2.5k): genuinely priceable in
    principle -- it is the top rung of the win-total distribution -- but the
    conference sim exposes champion/top_n only, with no win-count distribution
    to read a 12-0 off. Left out rather than approximated.

PLACEHOLDERS ARE REAL AND MUST BE DROPPED. Polymarket pre-creates empty slots:
the National Champion event lists 109 markets of which only 17 name a real team
-- the rest are literally "Team A".."Team CM", plus bare "A"/"B"/"C" in other
events. They carry a seeded 0.50 price and no volume, which is exactly the
phantom-price shape this app has already been bitten by, so they are filtered
here at ingestion rather than left for the trading guard to catch downstream.
"Other" is dropped for the same reason: it is a real remainder bucket, but it
names no team the model can price.
"""
from __future__ import annotations

import json
import logging
import re

from app.clients.base import get_json
from app.clients.polymarket_client import extract_market_prices

log = logging.getLogger("polymarket_cfb_client")

GAMMA = "https://gamma-api.polymarket.com"

TAG_SLUGS = ("cfb", "ncaaf", "college-football", "ncaa-football")

# Event title fragment -> our market_type. Order matters: "National Champion"
# would also match "Conference to Win National Championship", so the conference
# variants are rejected first in _market_type_for.
_TITLE_TO_MARKET_TYPE = (
    ("Team to Make National Championship", "cfb_finalist"),
    ("CFB Playoff Top 4 Seeds", "cfb_top4_seed"),
    ("team to make Quarters", "cfb_quarterfinal"),
    ("team to make Semis", "cfb_semifinal"),
    ("Team Win Totals", "win_total"),
    ("National Champion", "cfb_national_champion"),
    ("Conference: Winner", "conference_champion"),
)

# Titles that contain a fragment above but are a DIFFERENT proposition.
_TITLE_REJECT = ("Conference to Win", "Conference of ", "Class of ")

# "Alabama 8.5+" -> ("Alabama", 8.5). Win-total rungs carry the line in the
# group item title; every other market type's group item title is just the team.
_WIN_TOTAL = re.compile(r"^(.*?)\s+(\d+(?:\.\d+)?)\+$")

# Unfilled slots. Covers both "Team A".."Team CM" and the bare single letters
# ("A", "B", "C", "A conference") other events use for the same purpose.
_PLACEHOLDER = re.compile(r"^(Team\s+)?[A-Z]{1,3}(\s+conference)?$", re.IGNORECASE)

# Not a team: the explicit remainder bucket.
_NOT_A_TEAM = {"other"}


def _market_type_for(title: str) -> str | None:
    if any(r in title for r in _TITLE_REJECT):
        return None
    for fragment, market_type in _TITLE_TO_MARKET_TYPE:
        if fragment in title:
            return market_type
    return None


def get_open_events() -> list[dict]:
    """Deduped union of the four CFB tags, keyed by event slug."""
    seen: dict[str, dict] = {}
    for tag in TAG_SLUGS:
        try:
            events = get_json(f"{GAMMA}/events?tag_slug={tag}&closed=false&limit=100")
        except Exception:
            log.exception("polymarket cfb fetch failed for tag %s", tag)
            continue
        for ev in events or []:
            slug = ev.get("slug")
            if slug:
                seen.setdefault(slug, ev)
    return list(seen.values())


def get_cfb_futures_markets() -> list[dict]:
    """One row per (event, team). `team_name` is Polymarket's own full display
    name with mascot ("Boston College Eagles") -- the caller resolves it to an
    ESPN abbreviation via market_matcher_cfb.resolve_team, which already handles
    this shape. `line` is set for win_total rungs only."""
    rows: list[dict] = []
    for ev in get_open_events():
        title = ev.get("title") or ""
        market_type = _market_type_for(title)
        if market_type is None:
            continue
        for m in ev.get("markets") or []:
            label = (m.get("groupItemTitle") or "").strip()
            if not label:
                continue
            name, line = label, None
            wt = _WIN_TOTAL.match(label)
            if wt:
                name = wt.group(1).strip()
                try:
                    line = float(wt.group(2))
                except ValueError:
                    continue
            if _PLACEHOLDER.match(name) or name.lower() in _NOT_A_TEAM:
                continue
            if market_type == "win_total" and line is None:
                # A win-total rung with no parsable line has no proposition to
                # price -- skip rather than store a line-less ladder row, the
                # unactionable-bet shape flagged on the WNBA spread.
                continue
            prices = extract_market_prices(m)
            rows.append({
                "event_slug": ev.get("slug"),
                "event_title": title,
                "market_type": market_type,
                "team_name": name,
                "line": line,
                "condition_id": prices["condition_id"],
                "slug": prices["slug"],
                "question": prices["question"],
                "outcomes": prices["outcomes"],
                "outcome_prices": prices["outcome_prices"],
                "best_bid": prices["best_bid"],
                "best_ask": prices["best_ask"],
                "last_trade_price": prices["last_trade_price"],
                "volume": prices["volume"],
            })
    return rows
