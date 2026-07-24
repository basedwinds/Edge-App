"""Scheduled sanity check for the "dead/decided market shown as a live
recommendation" bug class -- found and fixed THREE separate times in one
session (2026-07-19), each with a different root cause (Tennis: Polymarket's
per-market closed flag ignored; MLB: a postponed-game ticker matched to the
wrong game; MMA: Kalshi's own pre-fight time estimate going stale post-event).
Each was only caught because a user happened to notice a bogus price live --
this runs the same "extreme price + large model disagreement + a real
suggested stake" signature that caught all three, on a schedule, so the next
occurrence gets logged automatically instead of waiting for another report.

Extended to CS2/Valorant/LoL 2026-07-20 -- this job originally only covered
the 6 non-esports sports (an oversight, not a deliberate scoping decision;
esports has its own per-poller dead-market handling, e.g. poller_cs2.py/
poller_valorant.py/poller_lol.py's own occurrence_datetime-staleness fix, but
was never wired into this cross-sport automated watchdog). Same signature,
same reasoning applies identically -- esports match_label rows have the same
shape as tennis/soccer's.

Deliberately reuses each sport's own ROUTER function directly (in-process,
not a second HTTP round trip) rather than re-implementing the pricing/model
logic here -- there is exactly one place per sport that computes implied_prob/
model_prob/kelly_fraction, and this check must see the SAME numbers the UI
does or it isn't testing anything real.

This is a detection/logging aid, not a fix -- it doesn't hide or exclude
anything from the actual API responses (that's each router's own job). A hit
here means investigate the same way the three bugs above were found: check
the specific market's real live platform status/result directly."""
import logging

from app.db.database import SessionLocal

log = logging.getLogger("dead_market_sanity_check")

# Same thresholds used to hunt down all three real bugs this session: a
# price sitting near one of Kalshi/Polymarket's own extremes, paired with a
# model estimate far enough away that quarter-Kelly still produced a real
# suggested stake -- exactly what a user would see as a live "opportunity."
_EXTREME_IMPLIED_LOW = 0.02
_EXTREME_IMPLIED_HIGH = 0.98
_MIN_DISAGREEMENT_PP = 0.15


def _flag(rows, sport: str, label_attr: str, id_attr: str = "id") -> list[str]:
    hits = []
    for r in rows:
        implied, model, kelly = r.implied_prob, r.model_prob, r.kelly_fraction
        if implied is None or model is None or kelly is None:
            continue
        if not (implied < _EXTREME_IMPLIED_LOW or implied > _EXTREME_IMPLIED_HIGH):
            continue
        if abs(model - implied) < _MIN_DISAGREEMENT_PP:
            continue
        label = getattr(r, label_attr, None) or "?"
        hits.append(
            f"{sport} id={getattr(r, id_attr)} '{label}' market_type={r.market_type} "
            f"source={r.source} implied={implied:.3f} model={model:.3f}"
        )
    return hits


def run_dead_market_sanity_check() -> list[str]:
    """Returns every hit found (also logged as a warning) -- callers besides
    the scheduler (e.g. a future manual/debug entrypoint) can inspect the
    list directly rather than re-parsing logs."""
    from app.api.routers.cs2_markets import list_cs2_markets
    from app.api.routers.lol_markets import list_lol_markets
    from app.api.routers.markets import list_markets
    from app.api.routers.mlb_markets import list_mlb_markets
    from app.api.routers.mma_markets import list_mma_markets
    from app.api.routers.nba_markets import list_nba_markets
    from app.api.routers.soccer_markets import list_soccer_markets
    from app.api.routers.tennis_markets import list_tennis_markets
    from app.api.routers.valorant_markets import list_valorant_markets

    session = SessionLocal()
    all_hits: list[str] = []
    try:
        checks = [
            ("nfl", list_markets, "label"),
            ("nba", list_nba_markets, "game_label"),
            ("mlb", list_mlb_markets, "game_label"),
            ("mma", list_mma_markets, "fight_label"),
            ("tennis", list_tennis_markets, "match_label"),
            ("soccer", list_soccer_markets, "match_label"),
            ("valorant", list_valorant_markets, "match_label"),
            ("cs2", list_cs2_markets, "match_label"),
            ("lol", list_lol_markets, "match_label"),
        ]
        for sport, list_fn, label_attr in checks:
            try:
                rows = list_fn(session=session)
            except Exception:
                log.exception("%s: dead-market check itself failed to run", sport)
                continue
            all_hits.extend(_flag(rows, sport, label_attr))
    finally:
        session.close()

    if all_hits:
        log.warning(
            "dead-market sanity check: %d suspicious row(s) found -- extreme price + "
            "large model disagreement + a real suggested stake, same signature that "
            "caught this session's Tennis/MLB/MMA bugs:\n%s",
            len(all_hits), "\n".join(all_hits),
        )
    else:
        log.info("dead-market sanity check: clean, 0 suspicious rows across all 9 sports/titles")
    return all_hits
