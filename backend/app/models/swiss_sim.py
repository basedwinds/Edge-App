"""General Elo-seeded Swiss-stage qualification simulator.

Prices "will team X advance out of this Swiss stage?" markets (the esports
"qualify to stage N" inventory this app previously left unpriced, since the
single-elimination tournament_winner sim deliberately doesn't cover group/
Swiss formats -- see project memory). Sport-agnostic: it takes teams + ratings
and a format, so the same engine works for any CS2/Valorant/LoL Swiss stage.

Swiss format (the standard CS2/Valorant "Buchholz Swiss", e.g. 16 teams ->
8 advance): every team plays each round paired against another team with the
SAME win-loss record; a team that reaches `wins_to_advance` wins qualifies, a
team that reaches `losses_to_eliminate` losses is out. With the canonical
first-to-3 / out-at-3 format on an even field this resolves in <=5 rounds and
advances EXACTLY half the teams -- a strong built-in correctness check (the
per-team advance probabilities sum to the number of qualifying slots).

Two honest simplifications, both flagged like the NFL season sim's own
tiebreaker note: (1) within a record group, matchups are seeded by Buchholz
difficulty (highest record-strength vs lowest), the real convention, with ties
broken by rating then randomly -- not the exact regional-seeding rules a
specific event might layer on; (2) an odd record group (never happens on the
standard even field, but guarded for robustness) gives the top-Buchholz team a
bye. model_validated: false, same as every model in this app.
"""
import random


def elo_win_prob(rating_a: float, rating_b: float) -> float:
    """Logistic Elo expectation -- P(A beats B) for a single map."""
    return 1.0 / (1.0 + 10 ** (-(rating_a - rating_b) / 400.0))


def _bo3_win_prob(p_map: float) -> float:
    """P(win a best-of-3) given a per-map win prob -- win 2 of 3. Compresses
    variance toward the favorite, the reason CS2 uses Bo3 for the matches that
    actually advance/eliminate a team."""
    return p_map * p_map * (3.0 - 2.0 * p_map)


def simulate_swiss(
    teams: list[tuple[str, float]],
    n_trials: int = 20000,
    wins_to_advance: int = 3,
    losses_to_eliminate: int = 3,
    bo3_deciders: bool = True,
    seed: int = 0,
) -> dict[str, float]:
    """teams: list of (name, elo_rating). Returns {name: advance_probability}.
    `bo3_deciders`: model advance/elimination matches (a win qualifies OR a
    loss eliminates) as best-of-3, everything else best-of-1 -- the standard
    CS2 Swiss rule. Set False to treat every match as a single map."""
    if not teams:
        return {}
    names = [t[0] for t in teams]
    rating = {t[0]: t[1] for t in teams}
    rng = random.Random(seed)
    advance_count = {n: 0 for n in names}

    for _ in range(n_trials):
        wins = {n: 0 for n in names}
        losses = {n: 0 for n in names}
        opponents: dict[str, list[str]] = {n: [] for n in names}
        active = set(names)
        advanced: set[str] = set()

        # <=  a few rounds; the guard just prevents a pathological infinite loop
        for _round in range(wins_to_advance + losses_to_eliminate + 2):
            if not active:
                break
            groups: dict[tuple[int, int], list[str]] = {}
            for n in active:
                groups.setdefault((wins[n], losses[n]), []).append(n)

            for rec, group in groups.items():
                # Buchholz difficulty = sum of each opponent's current wins;
                # seed high-vs-low within the group (rating then rng break ties).
                group.sort(key=lambda n: (sum(wins[o] for o in opponents[n]), rating[n], rng.random()), reverse=True)
                i, j = 0, len(group) - 1
                is_decider = (rec[0] == wins_to_advance - 1) or (rec[1] == losses_to_eliminate - 1)
                while i < j:
                    a, b = group[i], group[j]
                    p = elo_win_prob(rating[a], rating[b])
                    if bo3_deciders and is_decider:
                        p = _bo3_win_prob(p)
                    winner, loser = (a, b) if rng.random() < p else (b, a)
                    wins[winner] += 1
                    losses[loser] += 1
                    opponents[a].append(b)
                    opponents[b].append(a)
                    i += 1
                    j -= 1
                if i == j:  # odd leftover -> bye (rare; even fields never hit this)
                    wins[group[i]] += 1

            for n in list(active):
                if wins[n] >= wins_to_advance:
                    advanced.add(n)
                    active.discard(n)
                elif losses[n] >= losses_to_eliminate:
                    active.discard(n)

        for n in advanced:
            advance_count[n] += 1

    return {n: round(advance_count[n] / n_trials, 4) for n in names}
