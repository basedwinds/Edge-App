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
