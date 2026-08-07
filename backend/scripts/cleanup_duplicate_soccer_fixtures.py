"""One-time cleanup: delete SoccerMatch rows left empty by the La Liga
name-alias fix (2026-08-06).

Before the SP1 aliases landed, Kalshi's "Santander vs Villarreal" and
Polymarket's "Real Racing Club vs Villarreal CF" did not match each other, so
market_catalog_soccer.find_or_create_upcoming_match created a SECOND fixture row
for the same real match. Three SP1 fixtures were duplicated that way.

With the aliases in place the two spellings match, so the next poll of each
platform re-pointed its markets onto whichever row has the lower id, and the
other row is now sitting at zero markets. This removes those husks.

Deliberately conservative -- a row is only deleted when ALL of:
  * it has ZERO Market rows pointing at it (any status, not just active),
  * it has no recorded result (result_ft is NULL), and
  * a DIFFERENT row exists in the same league whose canonical team pair is
    identical, i.e. the real fixture genuinely survives elsewhere.
Anything failing a check is printed and left alone. --apply is required; the
default is a dry run.
"""
from __future__ import annotations

import sys

from app.db.database import SessionLocal
from app.db.models import Market, SoccerMatch
from app.ingestion.market_matcher_soccer import canonical_team_key


def main(apply: bool) -> int:
    session = SessionLocal()
    try:
        rows = session.query(SoccerMatch).all()
        by_key: dict[tuple[str, str, str], list[SoccerMatch]] = {}
        for r in rows:
            key = (r.league, canonical_team_key(r.home_team), canonical_team_key(r.away_team))
            by_key.setdefault(key, []).append(r)

        doomed: list[SoccerMatch] = []
        for key, group in sorted(by_key.items(), key=lambda kv: str(kv[0])):
            if len(group) < 2:
                continue
            for r in group:
                n_markets = session.query(Market).filter(Market.soccer_match_id == r.id).count()
                survivors = [
                    o for o in group
                    if o.id != r.id
                    and session.query(Market).filter(Market.soccer_match_id == o.id).count() > 0
                ]
                if n_markets == 0 and r.result_ft is None and survivors:
                    doomed.append(r)
                    print(f"  DELETE  id={r.id:<5} {key[0]} {r.home_team} vs {r.away_team} "
                          f"(0 markets; survivor id={survivors[0].id})")
                elif len(group) > 1:
                    print(f"  keep    id={r.id:<5} {key[0]} {r.home_team} vs {r.away_team} "
                          f"({n_markets} markets, result={r.result_ft!r})")

        print(f"\n{len(doomed)} row(s) qualify for deletion.")
        if not doomed:
            return 0
        if not apply:
            print("dry run -- re-run with --apply to delete.")
            return 0
        for r in doomed:
            session.delete(r)
        session.commit()
        print(f"deleted {len(doomed)} orphaned duplicate fixture row(s).")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
