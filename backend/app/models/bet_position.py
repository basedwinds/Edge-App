"""Which side of the contract a bet is on, and what that means at settlement.

`kelly_fraction` refuses negative edge, so for this app's whole history every
placed bet has been a YES. #186 measured the other half of the board and found
it pays (NO +16.0% [+10.5,+21.5] vs YES +15.2%, control arm ~0 both sides), so
`PlacedBet.position` now exists and settlement has to respect it.

WHY THIS IS A SHARED HELPER AND NOT FIVE EDITS. Five separate places assign a
bet's won/lost status:

    app/models/bet_settlement.py:1269            (per-sport graders, final score)
    app/models/kalshi_settlement.py:231          (Kalshi market result)
    app/models/polymarket_settlement.py:228      (Polymarket resolution price)
    app/ingestion/market_resolution_settlement.py:295
    app/ingestion/polymarket_settlement.py:201

The dominant defect in this codebase is a guard wired to some call sites and not
others -- it has been found repeatedly (the spread guard reached 3 of 13 routers;
the duplicate-listing cap 4 of 13). A NO bet that reaches a site which has not
been updated does not fail loudly: it silently grades as its own opposite, and
the wrong result then propagates into ROI, hit-rate and CLV. One function, five
callers, is the only shape where "did I get them all" is checkable by grep.

PUSH AND VOID ARE POSITION-INVARIANT and are deliberately passed through
untouched. A void refunds the stake regardless of which side was held, and a
push is a tie on the line -- neither has an "opposite". Only won/lost flip.

DELIBERATELY TAKES THE BET, NOT A STRING. Every call site already has the bet
object in hand, and reading `bet.position` inside here means a caller cannot
forget to pass it -- the failure mode would be a silent default to YES, which is
exactly the bug this module exists to prevent.
"""

YES = "yes"
NO = "no"


def is_no_side(bet) -> bool:
    """True if this bet is held on the NO side of the contract.

    Tolerates a missing/None position: every row written before the column
    existed is a YES bet by construction, because the app could not surface a
    NO. That default is a fact about the data, not a guess."""
    return (getattr(bet, "position", None) or YES) == NO


def resolve_status_for_position(bet, yes_frame_status: str | None) -> str | None:
    """Map a YES-frame outcome onto the side this bet actually holds.

    `yes_frame_status` is what the grader determined about the MARKET -- did the
    YES outcome happen -- in the vocabulary the call sites already use
    ("won" / "lost" / "push" / "void" / None). A NO bet wins exactly when the
    YES outcome loses.

    Returns the status to store. None and unrecognised values pass through so a
    caller's own "could not resolve -> leave pending" branch keeps working."""
    if yes_frame_status not in ("won", "lost"):
        return yes_frame_status
    if not is_no_side(bet):
        return yes_frame_status
    return "lost" if yes_frame_status == "won" else "won"


def position_note(bet) -> str:
    """Suffix for settlement_note so a settled NO bet is self-explanatory in the
    tracker. Without it a NO bet on a market whose YES outcome happened reads as
    "auto-settled from Kalshi market result (yes)" next to a LOST status, which
    looks like a settlement bug and would send someone hunting for one."""
    return " [NO side -- wins when the outcome does NOT happen]" if is_no_side(bet) else ""
