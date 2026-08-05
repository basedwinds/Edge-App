/** How many legs of ONE futures group may carry a suggested stake.
 *
 * Not about mutual exclusivity -- that turned out not to be the problem.
 * Measured on the live board: the genuinely winner-take-all groups are all
 * safely priced (MLB World Series 5 legs summing to 0.27, Super Bowl 4 legs at
 * 0.17, AFC champion 4 at 0.21). Backing five contenders for a combined 27c is
 * a longshot portfolio, not a loss by construction -- that only happens if the
 * legs of one winner-take-all group sum above 1.00, and none do.
 *
 * The real hazard is CORRELATION. `mlb/win_total` had 16 staked legs and
 * `playoff_qualifier` had 10 -- one model, answering one question, about one
 * season, sixteen times. If that model is biased they are all wrong together,
 * so the sixteen positions carry roughly the risk of one conviction while
 * looking like a diversified book.
 *
 * The cross-sport shortlist has capped this at 3 since it was written
 * (Combined.tsx). The per-sport futures pages never did, which is where the
 * stacking came from. 4 here rather than 3 so the per-sport page, which is the
 * fuller view, stays slightly more permissive than the curated shortlist.
 *
 * VISIBILITY IS UNCHANGED. Every leg still renders; the ones past the cap
 * simply carry no suggested stake. Hiding markets would be the wrong fix --
 * you should still be able to see the whole ladder and judge it yourself.
 */
export const MAX_STAKED_LEGS_PER_GROUP = 4;

type GroupLike = {
  id: number;
  market_type: string;
  group_label?: string | null;
  edge?: number | null;
  suggested_stake_dollars?: number | null;
};

/** Ids allowed to keep their suggested stake: the highest-edge legs of each
 * (market_type, group_label), up to the cap. Legs that never had a stake don't
 * consume a slot -- the cap is on real exposure, not on rows. */
export function stakeableLegIds<T extends GroupLike>(
  rows: T[],
  cap: number = MAX_STAKED_LEGS_PER_GROUP,
): Set<number> {
  const byGroup = new Map<string, T[]>();
  for (const r of rows) {
    if (r.suggested_stake_dollars == null) continue;
    const key = `${r.market_type}|${r.group_label ?? ""}`;
    const list = byGroup.get(key);
    if (list) list.push(r);
    else byGroup.set(key, [r]);
  }
  const allowed = new Set<number>();
  for (const list of byGroup.values()) {
    list.sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0));
    for (const r of list.slice(0, cap)) allowed.add(r.id);
  }
  return allowed;
}

/** Ladder market types: several rungs of the SAME question at different
 * thresholds, one team at a time.
 *
 * COL 35+ wins, 40+ wins and 45+ wins are not three opinions -- they are one
 * opinion about Colorado, stated three times, and they are NESTED: 45+ implies
 * 40+ implies 35+. Staking all three is a single directional view tripled, with
 * the added twist that the rungs cannot all lose independently.
 *
 * The game side already collapses ladders this way (see GAME_LADDER_MARKET_TYPES
 * and tennisLadderKey) -- futures never got the same treatment because their
 * rungs arrive with group_label null, so the per-group cap sees every team's
 * win-total ladder as ONE group and can't tell teams apart.
 */
const LADDER_TYPES = new Set([
  "win_total", "exact_win_total", "wins_any", "team_points",
  "conference_regtop", "top_n", "division_wins", "h2h_wins",
]);

/** One rung per (market_type, team) for ladder markets -- the best-edge one.
 * Non-ladder futures pass through untouched. */
export function collapseLadderRungs<T extends GroupLike & { team?: string | null; line?: number | null }>(
  rows: T[],
): Set<number> {
  const best = new Map<string, T>();
  const keep = new Set<number>();
  for (const r of rows) {
    if (r.suggested_stake_dollars == null) continue;
    if (!LADDER_TYPES.has(r.market_type)) { keep.add(r.id); continue; }
    const key = `${r.market_type}|${r.team ?? ""}`;
    const cur = best.get(key);
    if (!cur || (r.edge ?? 0) > (cur.edge ?? 0)) best.set(key, r);
  }
  for (const r of best.values()) keep.add(r.id);
  return keep;
}
