"""Free, structured-data-only award-market signals (MVP, Coach of the Year,
DPOY, OPOY).

OROY/DROY (rookie awards) explicitly NOT attempted -- checked live
2026-07-16 whether nflverse's free draft_picks.csv has the current season's
rookie class to use as a quality proxy (career stats don't work for rookies
almost by definition -- they have ~0 NFL history before the season starts),
and it doesn't yet have any 2026 entries. Real, confirmed data gap, not an
assumption -- revisit once that file is updated.

DPOY: uses defensive_ratings.py's weighted counting-stat career score
(sacks/INTs/forced-fumbles/TFLs -- no clean EPA-style rate metric exists for
defense the way it does for offense) instead of an EPA rate, same
team_best_record_pct multiplier as MVP. Team resolution reuses
depth_chart_client.get_current_starters (the original POOLED, all-positions
function) rather than a defense-specific depth-chart lookup -- sufficient
since DPOY candidates are inherently defensive players by construction (an
offensive player would score ~0 defensive stats anyway, harmlessly), so no
new depth-chart infrastructure was needed for this one.

OPOY: same team_best_record_pct * player_quality formula as MVP, but pooled
across QB/RB/WR/TE (MVP is QB/RB only, since WR/TE essentially never win
MVP but frequently contend for OPOY) using qb_ratings.py's career EPA plus
skill_position_ratings.py's rushing AND receiving career EPA together.

Also explicitly does NOT attempt player-movement/business/culture markets
(trade rumors, stadium relocation, celebrity events) -- confirmed 2026-07-16
there is no free structured data source for any of these, and the
alternative (an LLM researching current news) was evaluated and declined:
it would introduce this app's first-ever recurring paid cost and its first
non-auto-refreshing "frozen guess" data point, breaking the "everything is
either live-model-derived or absent" property every other market in this
app has. Out of scope by design, not by neglect.

MVP: historically dominated by QB play + team success (occasional elite RB
candidate). Scored as team_best_record_pct * player_career_quality,
normalized across the currently-tracked candidates into a probability
distribution. NOT independently backtested (no historical "depth chart +
career EPA -> actual MVP winner" regression was run) -- a reasoned analogy
to how MVP voting is known to correlate, same "reasoned, not fitted"
honesty as this project's other rough constants.

Coach of the Year: genuinely harder to project PREseason, because the award
rewards SURPRISE overperformance, which is close to unpredictable by
definition before the season happens (if it were reliably predictable, it
wouldn't be a surprise). Best available real-data proxy: this season's
season_sim-projected expected wins MINUS last season's actual win total --
teams with the biggest projected jump score highest. Flagged as this
project's most speculative futures signal (same "acknowledged folk wisdom"
category as schedule_spot_rules.py) -- not because the mechanism is wrong,
but because "predicting a surprise" is an inherently soft target.
"""
from app.models.qb_ratings import _canonical_key

MIN_QUALITY_FLOOR = 0.01  # keeps a below-average season from scoring exactly zero/negative
MIN_COTY_IMPROVEMENT_FLOOR = 0.1  # same idea for projected win improvement


def _full_name_key(name: str) -> str:
    """Coach names are used FULL (both nflverse's home_coach/away_coach and
    Kalshi's candidate names are already full names, no "F.Last" PBP-style
    abbreviation to bridge) -- so, unlike player names, there's no reason to
    accept qb_ratings.py's lossy first-initial+lastname compromise here.
    Real bug this avoids, caught live 2026-07-16: Jim Harbaugh (Chargers)
    and John Harbaugh (Ravens) both canonicalize to "jharbaugh" under that
    scheme, so one coach's team silently overwrote the other's in the
    reverse-lookup dict and BOTH candidates got resolved to the SAME
    (wrong, for at least one of them) team."""
    return " ".join(name.lower().replace(".", " ").split())


def build_qb_rb_full_name_to_team(skill_position_starters: dict[str, dict[str, str]]) -> dict[str, str]:
    """Full-display-name-keyed (via _full_name_key) version of
    build_qb_rb_name_to_team, for resolving a candidate's TEAM specifically
    -- never for aligning against a PBP-keyed stat dict, which still needs
    the lossy first-initial+lastname key (see build_qb_rb_name_to_team).
    Real bug fixed 2026-07-16: the lossy key collides for real players
    (e.g. "Jameson Williams" and "Javonte Williams" both canonicalize to
    "jwilliams", so one player's row silently got the OTHER's team) -- same
    collision CLASS as the already-fixed Jim/John Harbaugh coach bug
    (_full_name_key was built for exactly that), just showing up for
    skill-position players here instead of coaches. Kalshi/Polymarket
    candidate names and depth-chart names are both already full display
    names, so there's no PBP "F.Last" abbreviation to bridge for team
    resolution specifically."""
    out: dict[str, str] = {}
    for team, positions in skill_position_starters.items():
        for pos in ("QB", "RB"):
            name = positions.get(pos)
            if name:
                out[_full_name_key(name)] = team
    return out


def build_offensive_skill_full_name_to_team(skill_position_starters: dict[str, dict[str, str]]) -> dict[str, str]:
    """Same fix as build_qb_rb_full_name_to_team, all four skill positions
    (QB/RB/WR/TE) -- use for OPOY/leader/season-stat-ladder team
    resolution."""
    out: dict[str, str] = {}
    for team, positions in skill_position_starters.items():
        for pos in ("QB", "RB", "WR", "TE"):
            name = positions.get(pos)
            if name:
                out[_full_name_key(name)] = team
    return out


def build_all_starters_full_name_to_team(pooled_starters_by_team: dict[str, set[str]]) -> dict[str, str]:
    """Full-name-keyed version of build_all_starters_name_to_team, for DPOY
    team resolution -- see build_qb_rb_full_name_to_team's docstring. The
    POOLED starters set (many players per team, not one per position) makes
    a first-initial+lastname collision considerably MORE likely here than
    for the single-WR-slot case that surfaced the bug, not less."""
    out: dict[str, str] = {}
    for team, names in pooled_starters_by_team.items():
        for name in names:
            out[_full_name_key(name)] = team
    return out


def resolve_player_candidate_team_full_name(candidate_name: str, name_to_team: dict[str, str]) -> str | None:
    """Full-name-key counterpart of resolve_player_candidate_team -- use
    wherever team is resolved for display/scoring purposes (not aligning
    against a PBP-keyed stat dict)."""
    return name_to_team.get(_full_name_key(candidate_name))


def build_qb_rb_name_to_team(skill_position_starters: dict[str, dict[str, str]]) -> dict[str, str]:
    """Reverse of depth_chart_client.get_skill_position_starters --
    {canonical_key(player_name): team}, QB/RB only (the two positions MVP
    candidates realistically come from). Player names DO need
    qb_ratings.py's first-initial+lastname key (bridges PBP's "F.Last"
    passer/rusher-name convention against the full names depth charts use),
    unlike coaches -- see _full_name_key above for why the two positions use
    different matching."""
    out: dict[str, str] = {}
    for team, positions in skill_position_starters.items():
        for pos in ("QB", "RB"):
            name = positions.get(pos)
            if name:
                out[_canonical_key(name)] = team
    return out


def build_offensive_skill_name_to_team(skill_position_starters: dict[str, dict[str, str]]) -> dict[str, str]:
    """Same as build_qb_rb_name_to_team but ALL FOUR skill positions
    (QB/RB/WR/TE) -- needed for OPOY (which realistically includes WR/TE
    candidates, unlike MVP) and the receiving-stat leader/season-ladder
    markets. Real bug fixed 2026-07-16: OPOY and the receiving-stat
    leader/season markets had all been resolving team via
    build_qb_rb_name_to_team (QB/RB only), so every WR/TE candidate
    silently failed to resolve a team -- caught via an unusually low
    team-resolution rate in a live poller log (6/181 for season_rec_tds)
    rather than by design, same "verify the live numbers, don't just trust
    the code compiles" discipline as every other bug caught this session."""
    out: dict[str, str] = {}
    for team, positions in skill_position_starters.items():
        for pos in ("QB", "RB", "WR", "TE"):
            name = positions.get(pos)
            if name:
                out[_canonical_key(name)] = team
    return out


def build_coach_name_to_team(coach_by_team: dict[str, str]) -> dict[str, str]:
    """{full_name_key(coach_name): team} from each team's currently-listed
    head coach (nflverse publishes coach names for future games, same source
    coach_rules.py already uses for in-season change detection)."""
    return {_full_name_key(coach): team for team, coach in coach_by_team.items() if coach}


def resolve_player_candidate_team(candidate_name: str, name_to_team: dict[str, str]) -> str | None:
    return name_to_team.get(_canonical_key(candidate_name))


def resolve_coach_candidate_team(candidate_name: str, name_to_team: dict[str, str]) -> str | None:
    return name_to_team.get(_full_name_key(candidate_name))


def expected_wins(win_count_pct: list[float]) -> float:
    return sum(i * p for i, p in enumerate(win_count_pct))


def compute_mvp_scores(
    candidate_names: list[str],
    name_to_team: dict[str, str],
    sim_results: dict[str, dict],
    qb_stats: dict,
    rush_stats: dict,
) -> dict[str, float]:
    """Returns {canonical_key(candidate_name): probability}, normalized to
    sum to 1 ACROSS ONLY the candidates this function could actually score
    -- candidates it can't resolve to a team or a career-quality number are
    simply absent from the result (caller treats a missing key as "no model
    estimate", same convention as everywhere else in this app).

    `name_to_team` must be FULL-name-keyed (build_qb_rb_full_name_to_team),
    not the lossy first-initial+lastname dict -- team resolution and the
    qb_stats/rush_stats lookup use two DIFFERENT keys on purpose (fixed
    2026-07-16, see build_qb_rb_full_name_to_team's docstring): the stat
    dicts are PBP-derived and genuinely need the lossy key to bridge PBP's
    "F.Last" naming, but team resolution doesn't, and using the lossy key
    for team caused real collisions between different players."""
    raw: dict[str, float] = {}
    for name in candidate_names:
        key = _canonical_key(name)
        team = name_to_team.get(_full_name_key(name))
        if team is None:
            continue
        team_sim = sim_results.get(team)
        if team_sim is None:
            continue
        team_success = team_sim.get("best_record_pct", 0.0)

        player_quality = None
        if key in qb_stats:
            player_quality = qb_stats[key].get("epa_per_dropback")
        elif key in rush_stats:
            player_quality = rush_stats[key].get("epa_per_play")
        if player_quality is None:
            continue

        raw[key] = team_success * max(player_quality, MIN_QUALITY_FLOOR)

    total = sum(raw.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in raw.items()}


def compute_opoy_scores(
    candidate_names: list[str],
    name_to_team: dict[str, str],
    sim_results: dict[str, dict],
    qb_stats: dict,
    rush_stats: dict,
    recv_stats: dict,
) -> dict[str, float]:
    """Same formula and normalization as compute_mvp_scores, but pooled
    across QB/RB/WR/TE (recv_stats covers both WR and TE -- see
    skill_position_ratings.py, which doesn't distinguish position when
    tallying by receiver_player_name, so no extra plumbing needed for TE
    specifically). `name_to_team` must be FULL-name-keyed
    (build_offensive_skill_full_name_to_team) -- see compute_mvp_scores'
    docstring for why team resolution and stat-dict lookup use different
    keys."""
    raw: dict[str, float] = {}
    for name in candidate_names:
        key = _canonical_key(name)
        team = name_to_team.get(_full_name_key(name))
        if team is None:
            continue
        team_sim = sim_results.get(team)
        if team_sim is None:
            continue
        team_success = team_sim.get("best_record_pct", 0.0)

        player_quality = None
        if key in qb_stats:
            player_quality = qb_stats[key].get("epa_per_dropback")
        elif key in rush_stats:
            player_quality = rush_stats[key].get("epa_per_play")
        elif key in recv_stats:
            player_quality = recv_stats[key].get("epa_per_play")
        if player_quality is None:
            continue

        raw[key] = team_success * max(player_quality, MIN_QUALITY_FLOOR)

    total = sum(raw.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in raw.items()}


def compute_coty_scores(
    candidate_names: list[str],
    name_to_team: dict[str, str],
    sim_results: dict[str, dict],
    last_season_wins: dict[str, int],
) -> dict[str, float]:
    """Same normalization convention as compute_mvp_scores. Uses
    _full_name_key, not qb_ratings._canonical_key -- see that function's
    docstring for the real Jim/John Harbaugh collision this avoids."""
    raw: dict[str, float] = {}
    for name in candidate_names:
        key = _full_name_key(name)
        team = name_to_team.get(key)
        if team is None:
            continue
        team_sim = sim_results.get(team)
        if team_sim is None or "win_count_pct" not in team_sim:
            continue
        prior = last_season_wins.get(team)
        if prior is None:
            continue

        projected = expected_wins(team_sim["win_count_pct"])
        raw[key] = max(projected - prior, MIN_COTY_IMPROVEMENT_FLOOR)

    total = sum(raw.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in raw.items()}


def build_all_starters_name_to_team(pooled_starters_by_team: dict[str, set[str]]) -> dict[str, str]:
    """Reverse of depth_chart_client.get_current_starters (the original
    POOLED, all-positions function -- names are already lowercased/period-
    stripped by that function's own _normalize_name, but re-applying
    qb_ratings._canonical_key on top still produces the correct
    first-initial+lastname key regardless, confirmed live 2026-07-16:
    get_current_starters's "tj watt" and PBP's raw "T.Watt" both canonicalize
    to "twatt"). Used for DPOY team resolution -- see this module's
    docstring for why no defense-specific depth-chart lookup was needed."""
    out: dict[str, str] = {}
    for team, names in pooled_starters_by_team.items():
        for name in names:
            out[_canonical_key(name)] = team
    return out


def compute_dpoy_scores(
    candidate_names: list[str],
    name_to_team: dict[str, str],
    sim_results: dict[str, dict],
    defensive_scores: dict[str, float],
) -> dict[str, float]:
    """Same team_best_record_pct multiplier as MVP/OPOY, but player_quality
    comes from defensive_ratings.py's weighted counting-stat score (no clean
    EPA-style rate exists for defense) -- otherwise identical shape/
    normalization convention. `name_to_team` must be FULL-name-keyed
    (build_all_starters_full_name_to_team) -- see compute_mvp_scores'
    docstring; the POOLED starters set makes a lossy-key collision here
    considerably MORE likely than the single-WR-slot case that surfaced the
    bug, not less."""
    raw: dict[str, float] = {}
    for name in candidate_names:
        key = _canonical_key(name)
        team = name_to_team.get(_full_name_key(name))
        if team is None:
            continue
        team_sim = sim_results.get(team)
        if team_sim is None:
            continue
        team_success = team_sim.get("best_record_pct", 0.0)

        player_quality = defensive_scores.get(key)
        if player_quality is None:
            continue

        raw[key] = team_success * max(player_quality, MIN_QUALITY_FLOOR)

    total = sum(raw.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in raw.items()}
