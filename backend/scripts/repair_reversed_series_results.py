"""Find esports fixtures whose result was stored BACKWARDS, fix them, and
re-settle the bets that were graded off the reversed value.

THE BUG, user-reported 2026-08-09: a $10 Valorant bet on GIANTX GC showed as a
LOSS with the note "FALKE VENOM 2-0 GIANTX GC". vlr.gg says GIANTX GC 2-0 FALKE
VENOM. Cause is in market_catalog_{valorant,cs2,lol}.upsert -- see
app/ingestion/series_orientation.py for the full write-up. In short: the
scraped row was reconciled onto an existing fixture by an ORDER-INSENSITIVE
name match, then maps_won_a/maps_won_b/winner were written POSITIONALLY, so a
row listing the sides the other way round put the winner's maps in the loser's
column. That is fixed going forward; this repairs the rows already written.

HOW A REVERSAL IS PROVEN, rather than assumed. Map markets settle from the
PLATFORM's own resolution (Kalshi result=yes/no, Polymarket payout), which is
authoritative and completely independent of our scrape. So the app already
holds a second opinion on every match with graded map bets:

    match 342   Kalshi: GIANTX GC won map 1 AND map 2
                ours:   GIANTX GC won 0 maps, FALKE VENOM won 2-0

A team cannot win two maps of a Bo3 and lose. Any match where a team has
platform-confirmed map wins but we recorded ZERO maps for it, while recording
the OPPONENT as winner, is reversed.

WHAT IS DELIBERATELY NOT FLAGGED: a team with MORE platform-confirmed map wins
than we recorded but still non-zero. That is the ordinary case of one map being
priced on both Kalshi and Polymarket, so it settles twice -- match 50 (G2 Gozen
"4" wins in a Bo3) and LoL 176 (Verdant "3" in a Bo3) are both that, and both
were confirmed CORRECT against vlr.gg. Requiring zero is what separates a
double-count from a reversal.

RE-SETTLEMENT is limited to bets graded from the scraped result -- their note
starts with "auto-settled: ". Bets already graded from a platform resolution
were right all along and are left untouched, because the platform is the
authority and re-deriving them from our scrape would be a downgrade.

Run with --apply to write. Default is a dry run.
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import Cs2Match, LolMatch, PlacedBet, ValorantMatch  # noqa: E402

TITLES = {
    "valorant": (ValorantMatch, "valorant_match_id"),
    "cs2": (Cs2Match, "cs2_match_id"),
    "lol": (LolMatch, "lol_match_id"),
}
# Notes written by a platform's own resolution -- authoritative, never re-graded.
PLATFORM_NOTES = ("from Kalshi market resolution", "from Polymarket resolution")
SCRAPER_NOTE_PREFIX = "auto-settled: "


def norm(s: str | None) -> str:
    return (s or "").strip().lower()


def main() -> None:
    apply = "--apply" in sys.argv
    session = SessionLocal()
    flipped_total = resettled_total = 0
    try:
        for sport, (model, fk) in TITLES.items():
            bets = session.query(PlacedBet).filter(
                PlacedBet.sport == sport, getattr(PlacedBet, fk).isnot(None)).all()
            by_match: dict[int, list[PlacedBet]] = collections.defaultdict(list)
            for b in bets:
                by_match[getattr(b, fk)].append(b)

            for mid, mbets in sorted(by_match.items()):
                match = session.get(model, mid)
                if match is None or match.winner is None:
                    continue
                if match.maps_won_a is None or match.maps_won_b is None:
                    continue

                # platform-confirmed map wins per team name
                confirmed: collections.Counter = collections.Counter()
                for b in mbets:
                    if b.market_type != "map_winner" or b.status != "won" or not b.team:
                        continue
                    if any(n in (b.settlement_note or "") for n in PLATFORM_NOTES):
                        confirmed[norm(b.team)] += 1
                if not confirmed:
                    continue

                ours = {norm(match.team_a): match.maps_won_a, norm(match.team_b): match.maps_won_b}
                winner_name = norm(match.team_a if match.winner == "team_a" else match.team_b)
                reversed_ = [t for t, n in confirmed.items()
                             if t in ours and ours[t] == 0 and n >= 1 and t != winner_name]
                if not reversed_:
                    continue

                print(f"REVERSED {sport} match {mid}: stored {match.team_a} {match.maps_won_a}-"
                      f"{match.maps_won_b} {match.team_b} (winner {winner_name})")
                for t in reversed_:
                    print(f"     but the platform confirms {t!r} won {confirmed[t]} map(s)")

                match.maps_won_a, match.maps_won_b = match.maps_won_b, match.maps_won_a
                match.winner = "team_b" if match.winner == "team_a" else "team_a"
                new_winner = norm(match.team_a if match.winner == "team_a" else match.team_b)
                print(f"     -> {match.team_a} {match.maps_won_a}-{match.maps_won_b} "
                      f"{match.team_b} (winner {new_winner})")
                flipped_total += 1

                for b in mbets:
                    note = b.settlement_note or ""
                    if not note.startswith(SCRAPER_NOTE_PREFIX):
                        continue  # graded by the platform, or still pending
                    if b.market_type != "series_winner" or not b.team:
                        continue
                    old = b.status
                    b.status = "won" if norm(b.team) == new_winner else "lost"
                    b.settlement_note = (
                        f"re-settled: stored result was reversed (see "
                        f"scripts/repair_reversed_series_results.py); "
                        f"{match.team_a} {match.maps_won_a}-{match.maps_won_b} {match.team_b}"
                    )
                    if old != b.status:
                        resettled_total += 1
                        print(f"     bet {b.id} ({b.source}, ${b.stake_dollars or 0:.2f}) "
                              f"{old} -> {b.status}")

        if apply:
            session.commit()
            print(f"\nAPPLIED: {flipped_total} fixture(s) corrected, {resettled_total} bet(s) re-settled")
        else:
            session.rollback()
            print(f"\nDRY RUN: would correct {flipped_total} fixture(s) and re-settle "
                  f"{resettled_total} bet(s). Re-run with --apply to write.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
