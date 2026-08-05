import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { RecommendedBetsTable } from "../components/markets/RecommendedBetsTable";
import { BetReasoningModal } from "../components/markets/BetReasoningModal";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import {
  fetchSettings,
  PORTFOLIO_CEILING_PCT,
  fetchMarkets, buildRecommendedBets,
  fetchNbaMarkets, buildNbaRecommendedBets,
  fetchWnbaMarkets, buildWnbaRecommendedBets,
  fetchCfbMarkets, buildCfbRecommendedBets,
  fetchMlbMarkets, buildMlbRecommendedBets,
  fetchMmaMarkets, buildMmaRecommendedBets,
  fetchTennisMarkets, buildTennisRecommendedBets,
  fetchSoccerMarkets, buildSoccerRecommendedBets,
  fetchValorantMarkets, buildValorantRecommendedBets,
  fetchCs2Markets, buildCs2RecommendedBets,
  fetchLolMarkets, buildLolRecommendedBets,
  fetchRacingMarkets, buildRacingRecommendedBets,
  markBetPlaced,
  fetchFutures, fetchNbaFutures, fetchMlbFutures, fetchTennisFutures,
  fetchSoccerFutures, fetchValorantFutures, fetchCs2Futures, fetchLolFutures,
  type RecommendedBetRow,
  type SettingsPayload,
} from "../api/markets";
import { CrossSportFuturesTable, type CrossSportFuturesRow } from "../components/markets/CrossSportFuturesTable";
import { LADDER_TYPES } from "../utils/futuresGroupCap";
import { fetchReadiness, isRowNotReady } from "../api/markets";
import { fetchOpenBets, fetchSettledBets } from "../api/markets";
import type { FuturesMarketRow } from "../types/market";

// One place to see every sport's recommended bets at once. Each sport is built
// with its OWN pool sizes (from Settings), and each sport's ceiling nets off
// the capital already committed to its PENDING bets, so a full sport stops
// suggesting until something settles. Rows are then merged and sorted by
// suggested stake. Mark bets from here or from the per-sport page, both
// route by row.sport.
//
// Every fetch is timeout-guarded (a single heavy endpoint -- e.g. NFL's
// /markets, which can return tens of thousands of rows against a bloated
// snapshot table -- must not hang or block the whole combined view). A fetch
// that errors or exceeds the budget contributes nothing rather than failing
// the page; the sports that DID load still render.
// Last successful result per feed, so a slow or failed one degrades to STALE
// rather than to EMPTY.
//
// REAL BUG this fixes (user-reported 2026-08-04: "bets show up and disappear").
// Every feed here is wrapped so one slow sport cannot hold up the page -- but on
// timeout the fallback was `[]`, which reads as "this sport has no bets today"
// and makes the whole block vanish, then reappear on the next poll once the
// backend cache is warm again. Tennis was the one crossing the line: measured at
// 24.5s to recompute (and 35s while the startup pollers were competing for the
// DB) against an 18s budget. The recompute itself has since been cut to ~4.5s,
// but latency is not something this page can guarantee, so the failure mode is
// fixed here too: showing the previous list for a few more seconds is honest and
// stable, showing an empty one is neither.
const lastGood = new Map<string, unknown>();

function guard<T>(key: string, p: Promise<T>, fallback: T, ms = 18000): Promise<T> {
  const remembered = () => (lastGood.has(key) ? (lastGood.get(key) as T) : fallback);
  return Promise.race([
    p.then((v) => {
      lastGood.set(key, v);
      return v;
    }).catch(remembered),
    new Promise<T>((res) => setTimeout(() => res(remembered()), ms)),
  ]);
}

/** One sport's ranked-but-uncapped shortlist plus the dollars it may use.
 *
 * The portfolio ceiling is deliberately NOT applied here. It has to be applied
 * after the start-time window, or "Today" degenerates into "whichever of the
 * global top-N happen to start today" -- which is how a day with 69 qualified
 * tennis candidates rendered zero of them (user-reported 2026-08-04). */
type SportPlan = { ranked: RecommendedBetRow[]; ceilings: Record<"weekly" | "futures", number> };

/** Total live exposure allowed across ALL sports, as a share of bankroll.
 *
 * The per-sport ceilings alone cannot bound this: twelve sports at 6 slots
 * each sum to $719 on a $2,000 bankroll (36%), and nothing stopped them
 * stacking. That is fine on a Tuesday when three sports have candidates and
 * fatal on a Saturday when CFB, NFL, MMA and racing all land at once -- the
 * exact case a per-sport cap is blind to. This is the only number that
 * bounds total risk, so it is the one to change if the appetite changes.
 *
 * Futures are separate and smaller because they do NOT recycle: a season
 * future locks capital until spring, so it cannot share a budget sized
 * around turnover. */
const GLOBAL_CAP_PCT = { weekly: 0.30, futures: 0.10 } as const;

type CombinedPlan = { plans: SportPlan[]; globalCeilings: Record<"weekly" | "futures", number> };

async function loadCombined(): Promise<CombinedPlan> {
  const s = await fetchSettings();
  // Game/match markets only -- this is an "upcoming bets" view, and the
  // per-sport /futures endpoints are both the slowest (season-long models +
  // depth-chart lookups) and season-long, not "upcoming". They stay on each
  // sport's own Futures page. `[]` is passed for the futures arg below, so
  // every row here draws on the WEEKLY pool.
  // Capital already committed to PENDING bets. Subtracted from each sport's
  // ceiling below, so a sport that is full stops producing suggestions until
  // something settles and frees room. Every builder has taken a
  // `lockedWeeklyDollars` argument since it was written and NO caller in the
  // app ever supplied one -- the cap has never actually been enforced.
  const openBets = await guard("OpenBetsForPool", fetchOpenBets(), []);
  // Weekly and futures are SEPARATE sub-allocations (Settings splits them),
  // so they have to be tracked apart -- a season-long win-total row must not
  // eat the room reserved for tonight's games.
  const locked = new Map<string, { weekly: number; futures: number }>();
  for (const b of openBets) {
    const key = b.sport === "f1" || b.sport === "nascar" || b.sport === "irl" ? "__racing" : b.sport;
    const cur = locked.get(key) ?? { weekly: 0, futures: 0 };
    if (b.stake_pool === "futures") cur.futures += b.stake_dollars;
    else cur.weekly += b.stake_dollars;
    locked.set(key, cur);
  }

  const [
    nflM, nbaM, wnbaM, cfbM, mlbM, mmaM, tenM, socM, valM, cs2M, lolM, racingM,
  ] = await Promise.all([
    guard("Markets", fetchMarkets(), []), guard("NbaMarkets", fetchNbaMarkets(), []),
    guard("WnbaMarkets", fetchWnbaMarkets(), []), guard("CfbMarkets", fetchCfbMarkets(), []),
    guard("MlbMarkets", fetchMlbMarkets(), []), guard("MmaMarkets", fetchMmaMarkets(), []),
    guard("TennisMarkets", fetchTennisMarkets(), []), guard("SoccerMarkets", fetchSoccerMarkets(), []), guard("ValorantMarkets", fetchValorantMarkets(), []),
    guard("Cs2Markets", fetchCs2Markets(), []), guard("LolMarkets", fetchLolMarkets(), []), guard("RacingMarkets", fetchRacingMarkets(), []),
  ]);
  const plan = (sport: string, ranked: RecommendedBetRow[], weeklyPool: number, futuresPool = 0): SportPlan => {
    const l = locked.get(sport) ?? { weekly: 0, futures: 0 };
    return {
      ranked,
      ceilings: {
        weekly: Math.max(0, weeklyPool * PORTFOLIO_CEILING_PCT - l.weekly),
        futures: Math.max(0, futuresPool * PORTFOLIO_CEILING_PCT - l.futures),
      },
    };
  };
  // Global ceilings net off EVERYTHING already committed, same as the
  // per-sport ones -- otherwise the total cap only binds on new capital.
  let liveWeekly = 0, liveFutures = 0;
  for (const b of openBets) {
    if (b.stake_pool === "futures") liveFutures += b.stake_dollars;
    else liveWeekly += b.stake_dollars;
  }
  const globalCeilings = {
    weekly: Math.max(0, s.bankroll_dollars * GLOBAL_CAP_PCT.weekly - liveWeekly),
    futures: Math.max(0, s.bankroll_dollars * GLOBAL_CAP_PCT.futures - liveFutures),
  };
  const plans = [
    plan("nfl", buildRecommendedBets(nflM, [], s.weekly_pool_dollars, s.futures_pool_dollars).ranked, s.weekly_pool_dollars, s.futures_pool_dollars),
    plan("nba", buildNbaRecommendedBets(nbaM, [], s.nba_weekly_pool_dollars, s.nba_futures_pool_dollars).ranked, s.nba_weekly_pool_dollars, s.nba_futures_pool_dollars),
    plan("wnba", buildWnbaRecommendedBets(wnbaM, s.wnba_weekly_pool_dollars, s.wnba_futures_pool_dollars).ranked, s.wnba_weekly_pool_dollars, s.wnba_futures_pool_dollars),
    plan("cfb", buildCfbRecommendedBets(cfbM, s.cfb_weekly_pool_dollars, s.cfb_futures_pool_dollars).ranked, s.cfb_weekly_pool_dollars, s.cfb_futures_pool_dollars),
    plan("mlb", buildMlbRecommendedBets(mlbM, [], s.mlb_weekly_pool_dollars, s.mlb_futures_pool_dollars).ranked, s.mlb_weekly_pool_dollars, s.mlb_futures_pool_dollars),
    plan("mma", buildMmaRecommendedBets(mmaM, s.mma_weekly_pool_dollars).ranked, s.mma_weekly_pool_dollars),
    plan("tennis", buildTennisRecommendedBets(tenM, s.tennis_weekly_pool_dollars).ranked, s.tennis_weekly_pool_dollars),
    plan("soccer", buildSoccerRecommendedBets(socM, s.soccer_weekly_pool_dollars).ranked, s.soccer_weekly_pool_dollars),
    plan("valorant", buildValorantRecommendedBets(valM, s.valorant_weekly_pool_dollars, s.valorant_futures_pool_dollars).ranked, s.valorant_weekly_pool_dollars, s.valorant_futures_pool_dollars),
    plan("cs2", buildCs2RecommendedBets(cs2M, s.cs2_weekly_pool_dollars, s.cs2_futures_pool_dollars).ranked, s.cs2_weekly_pool_dollars, s.cs2_futures_pool_dollars),
    plan("lol", buildLolRecommendedBets(lolM, s.lol_weekly_pool_dollars, s.lol_futures_pool_dollars).ranked, s.lol_weekly_pool_dollars, s.lol_futures_pool_dollars),
    // Racing rows carry sport f1/nascar/irl but all three draw on ONE pool,
    // so the committed capital of all three has to net off together.
    plan("__racing", buildRacingRecommendedBets(racingM, s.racing_weekly_pool_dollars).ranked, s.racing_weekly_pool_dollars),
  ];
  return { plans, globalCeilings };
}

// Cross-sport futures for the "Futures" window: every sport's /futures merged,
// tagged with sport, filtered to edge-qualified (>=3pp model-vs-market gap),
// sorted by edge. WNBA/MMA have no futures, so they're omitted.
async function loadCombinedFutures(): Promise<CrossSportFuturesRow[]> {
  const [nfl, nba, mlb, ten, soc, val, cs2, lol] = await Promise.all([
    guard("Futures", fetchFutures(), []), guard("NbaFutures", fetchNbaFutures(), []), guard("MlbFutures", fetchMlbFutures(), []),
    guard("TennisFutures", fetchTennisFutures(), []), guard("SoccerFutures", fetchSoccerFutures(), []), guard("ValorantFutures", fetchValorantFutures(), []),
    guard("Cs2Futures", fetchCs2Futures(), []), guard("LolFutures", fetchLolFutures(), []),
  ]);
  const tag = (fr: FuturesMarketRow[], sport: CrossSportFuturesRow["sport"]) =>
    fr.map((r) => ({ ...r, sport }));
  const all: CrossSportFuturesRow[] = [
    ...tag(nfl, "nfl"), ...tag(nba, "nba"), ...tag(mlb, "mlb"), ...tag(ten, "tennis"),
    ...tag(soc, "soccer"), ...tag(val, "valorant"), ...tag(cs2, "cs2"), ...tag(lol, "lol"),
  ];
  // Curated to a number you can actually keep up with. Raw positive-edge is
  // ~800+ and mostly noise, so:
  //  1. drop the phantom player-stat/leader markets (naive last-season-rate
  //     projections that over-project -- the app already flags them tracking-only),
  //  2. require the market to have actually TRADED (a seeded price isn't bettable),
  //  3. POSITIVE edge >= 5pp (real disagreement, clears fee noise),
  //  4. dedup the same proposition across Kalshi/Polymarket (keep the bigger edge),
  //  5. cap to the top 3 per (sport, market_type) so no single ladder floods,
  //  6. global cap so the list stays short.
  // NOTE: placed futures are NOT excluded here. The table itself marks them
  // "Placed ✓" (matched by real-world proposition, so both a Kalshi and a
  // Polymarket copy of the same future count) -- excluding by market_id used to
  // hide the Kalshi copy while still showing the Polymarket one, so a future you
  // HAD placed kept re-appearing as unplaced. Showing the full list with a
  // placed badge is clearer and stops duplicate placements.
  const isPhantom = (mt: string) => mt.startsWith("season_") || mt.startsWith("leader_");
  const candidates = all
    .filter((r) => r.edge !== null && r.edge >= 0.05 && r.volume && !isPhantom(r.market_type))
    .sort((a, b) => b.edge! - a.edge!);

  const bestByProp = new Map<string, CrossSportFuturesRow>();
  for (const r of candidates) {
    // `line` is part of the identity for a normal proposition, but NOT for a
    // ladder: COL 35+ wins, 40+ and 45+ are one nested opinion about Colorado
    // (45+ implies 40+ implies 35+), and keying on line let all three through as
    // separate "props" -- which is exactly what showed up as COL three times.
    const ladder = LADDER_TYPES.has(r.market_type);
    const key = `${r.sport}|${r.market_type}|${r.team ?? ""}|${r.side ?? ""}|${ladder ? "" : r.line ?? ""}`;
    if (!bestByProp.has(key)) bestByProp.set(key, r); // candidates already sorted desc, first = best
  }

  const perType = new Map<string, number>();
  const out: CrossSportFuturesRow[] = [];
  for (const r of bestByProp.values()) {
    const tk = `${r.sport}|${r.market_type}`;
    const n = perType.get(tk) ?? 0;
    if (n >= 3) continue;
    perType.set(tk, n + 1);
    out.push(r);
  }
  out.sort((a, b) => b.edge! - a.edge!);
  // Per-SPORT cap so a sport deep in its season (MLB/soccer, lots of high-edge
  // futures) can't crowd the shortlist and bury a sport just entering its
  // season -- each sport gets up to its own top-N slots. Guarantees every ready
  // sport with futures gets airtime, whenever its season happens to start.
  const perSport = new Map<string, number>();
  const capped: CrossSportFuturesRow[] = [];
  for (const r of out) {
    const n = perSport.get(r.sport) ?? 0;
    if (n >= MAX_FUTURES_PER_SPORT) continue;
    perSport.set(r.sport, n + 1);
    capped.push(r);
  }
  return capped;
}

const MAX_FUTURES_PER_SPORT = 8; // futures shortlist: top-N per sport (fairness across sports)

const EMPTY_IDS: Set<string> = new Set();
const EMPTY_PLAN: CombinedPlan = { plans: [], globalCeilings: { weekly: 0, futures: 0 } };

function localDateStr(offsetDays = 0): string {
  return new Date(Date.now() + offsetDays * 86400000).toLocaleDateString("en-CA"); // YYYY-MM-DD, local
}

type WindowFilter = "today" | "2d" | "all" | "futures";
// ROLLING hours, not calendar days. A calendar boundary is the wrong cut for
// this board: 104 of 246 upcoming tennis matches (42%) start between 10pm and
// 6am local, and their start times get CORRECTED as real scraped schedules
// replace the platform's guess -- one such correction moved a match from
// 11:30pm to 5:00am the next day, which silently dropped it out of "Today"
// and reshuffled everything behind it. Rolling hours make that correction a
// non-event: 11:30pm and 5:00am are both simply "soon". Labels renamed to
// match, since the tab no longer claims a calendar day it can't determine.
const WINDOW_HOURS: Partial<Record<WindowFilter, number>> = { today: 24, "2d": 48 };
const WINDOWS: { key: WindowFilter; label: string }[] = [
  { key: "today", label: "Next 24h" },
  { key: "2d", label: "Next 48h" },
  { key: "all", label: "All upcoming" },
  { key: "futures", label: "Futures" },
];

const SPORT_SHORT: Record<string, string> = {
  nfl: "NFL", nba: "NBA", wnba: "WNBA", cfb: "CFB", mlb: "MLB", mma: "MMA",
  tennis: "Tennis", soccer: "Soccer", valorant: "Valorant", cs2: "CS2", lol: "LoL",
  f1: "F1", nascar: "NASCAR", irl: "IndyCar",
};

/** How much of each sport's weekly pool is already committed to PENDING bets.
 *
 * The recommendation ceiling is computed per window and does NOT subtract
 * capital already locked, so nothing on this page stops you from placing the
 * top 2 Valorant bets off "All upcoming", switching to "Next 24h", and placing
 * its 2 as well -- $80 against a $40.80 ceiling, silently. This makes that
 * visible rather than enforcing it: hiding bets as you place them is exactly
 * the vanishing-row behaviour that made this list confusing in the first place.
 *
 * Interim. Real enforcement belongs in the staking layer, not a warning strip.
 */
function weeklyPoolFor(sport: string, s: SettingsPayload): number | null {
  const rec = s as unknown as Record<string, number | undefined>;
  if (sport === "nfl") return s.weekly_pool_dollars;                       // no nfl_ prefix
  if (sport === "f1" || sport === "nascar" || sport === "irl") return rec.racing_weekly_pool_dollars ?? null;
  return rec[`${sport}_weekly_pool_dollars`] ?? null;
}

function PoolExposure({ settings }: { settings: SettingsPayload }) {
  const q = useQuery({ queryKey: ["open-bets-exposure"], queryFn: fetchOpenBets });
  const rows = useMemo(() => {
    const open = (q.data ?? []).filter((b) => b.stake_pool !== "futures");
    const bySport = new Map<string, number>();
    for (const b of open) bySport.set(b.sport, (bySport.get(b.sport) ?? 0) + b.stake_dollars);
    return [...bySport.entries()]
      .map(([sport, committed]) => {
        const pool = weeklyPoolFor(sport, settings);
        const ceiling = pool === null ? null : pool * PORTFOLIO_CEILING_PCT;
        return { sport, committed, ceiling, pct: ceiling ? committed / ceiling : null };
      })
      .filter((r) => r.ceiling !== null)
      .sort((a, b) => (b.pct ?? 0) - (a.pct ?? 0));
  }, [q.data, settings]);

  if (rows.length === 0) return null;
  const over = rows.filter((r) => (r.pct ?? 0) > 1).length;
  return (
    <div className="mt-3 text-[11px] text-[var(--color-text-muted)]">
      <span className="mr-2">Pool already committed to open bets:</span>
      {rows.map((r) => {
        const pct = r.pct ?? 0;
        const tone = pct > 1 ? "text-[var(--color-critical)]"
          : pct > 0.8 ? "text-[var(--color-warning)]" : "text-[var(--color-text-dim)]";
        return (
          <span key={r.sport} className={`mr-2 whitespace-nowrap ${tone}`}>
            {SPORT_SHORT[r.sport] ?? r.sport} ${r.committed}/${Math.round(r.ceiling!)}
          </span>
        );
      })}
      {over > 0 && (
        <span className="block mt-1">
          {over} {over === 1 ? "sport is" : "sports are"} past the ceiling the recommendations size
          against — the list won't stop you, so treat those as full.
        </span>
      )}
    </div>
  );
}

export function Combined() {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["combined-recommended"], queryFn: loadCombined });
  const futuresQuery = useQuery({ queryKey: ["combined-futures"], queryFn: loadCombinedFutures });
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  // The table hides rows whose sport is out of season / outside its game
  // window. The headline tiles used the UNfiltered list, so they read "14
  // bets shown" above a table of 3. Filter once, here, so both agree.
  const readinessQuery = useQuery({ queryKey: ["readiness"], queryFn: fetchReadiness });
  // Market ids you've ALREADY placed (any pool), so the table shows "Placed" for
  // them even after navigating away and back -- the per-row marked state alone is
  // ephemeral component state and forgets on remount, which is why a bet you'd
  // marked kept re-appearing as un-placed (reported 2026-07-24). Derived from the
  // real placed-bet records, so it survives reloads.
  const placedQuery = useQuery({
    queryKey: ["placed-market-ids"],
    queryFn: async () => {
      const [open, settled] = await Promise.all([fetchOpenBets(), fetchSettledBets()]);
      // cross-platform proposition keys + game-level keys (see usePlacedKeys)
      const keys = new Set<string>();
      for (const b of [...open, ...settled]) {
        keys.add(b.cross_key);
        if (b.game_key) keys.add(`game:${b.game_key}`);
      }
      return keys;
    },
  });
  const placedMarketIds = placedQuery.data ?? EMPTY_IDS;
  const [reasoningRow, setReasoningRow] = useState<RecommendedBetRow | null>(null);
  const [win, setWin] = useState<WindowFilter>("all");

  const combined = query.data ?? EMPTY_PLAN;
  const plans = combined.plans;
  const windowed = useMemo(() => {
    // Bucket by the REAL start instant. Match sports (tennis/soccer/esports/MMA)
    // carry an accurate estimatedStartTime -- and a `gameday` derived from a
    // STALE match_date (tennis showed match_date 5 days before the real start),
    // so filtering on gameday silently dropped those bets from Today/Next-2-days
    // AND couldn't tell a game had already started (a 7am kickoff still showed
    // "today"). Prefer the timestamp; fall back to gameday's date only when no
    // timestamp exists (team sports, where gameday IS accurate). Reported
    // 2026-07-24.
    const now = Date.now();
    const hours = WINDOW_HOURS[win];
    const until = hours === undefined ? null : now + hours * 3600_000;
    const start = (r: RecommendedBetRow): { ms: number | null; date: string | null } => {
      if (r.estimatedStartTime) {
        const ms = Date.parse(r.estimatedStartTime);
        if (!Number.isNaN(ms)) return { ms, date: new Date(ms).toLocaleDateString("en-CA") };
      }
      return { ms: null, date: r.gameday };
    };
    const inWindow = (r: RecommendedBetRow) => {
      // Season-long markets belong on the Futures tab, not in a list of games
      // kicking off. They leak in because CFB and WNBA have no /futures endpoint
      // -- every other sport does -- so their win-total, playoff and conference
      // ladders come through the GAME feed with stake_pool "futures" and no game
      // date. Measured: 78 such rows (CFB win_total/cfb_playoff/
      // conference_champion, WNBA win_total). Excluded here by pool rather than
      // by a market-type list, so a new season market can't reintroduce it.
      if (r.stakePool === "futures") return false;
      const { ms, date } = start(r);
      if (ms !== null && ms < now) return false;        // already started -> not "upcoming"
      if (until === null) return true;                   // "all": futures (date null) live only here
      if (ms !== null) return ms <= until;               // real timestamp -> exact rolling window
      // Team sports carry a date but no usable time. Keep the day-level test for
      // them rather than inventing an hour: a date within the window's span of
      // days is as precise as the data allows.
      if (date === null) return false;                   // season-long futures: All-upcoming only
      return date >= localDateStr(0) && date <= localDateStr(Math.ceil(hours! / 24) - 1);
    };

    // The window is applied FIRST, then each sport's portfolio ceiling is spent
    // on what survived. Done the other way round -- ceiling first, window second
    // -- a narrow window shows only the leftovers of a global ranking, which is
    // how Today rendered 3 MLB bets and nothing else while 69 tennis candidates
    // sat qualified and unshown behind 3 higher-edge ones starting tomorrow.
    // Each window therefore answers "the best use of this pool on THIS slate",
    // and the same bet can appear in more than one window -- correct, since the
    // ceiling is about how much to deploy at once, not a running total.
    // PASS 1 -- each sport spends its OWN ceiling. This is what guarantees every
    // sport with real candidates is represented at all: without it a couple of
    // high-edge sports absorb the whole budget (measured on the live board: the
    // top 20 by raw edge contained zero MLB despite 21 qualifying candidates).
    let cut = 0;                 // qualified and in-window, but a ceiling was full
    const picked: RecommendedBetRow[] = [];
    for (const p of plans) {
      const spent = { weekly: 0, futures: 0 };
      for (const row of p.ranked) {
        if (!inWindow(row)) continue;
        const pool = row.stakePool;
        if (spent[pool] + row.suggestedStakeDollars > p.ceilings[pool]) { cut++; continue; }
        spent[pool] += row.suggestedStakeDollars;
        picked.push(row);
      }
    }

    // PASS 2 -- the GLOBAL cap, the only thing bounding total risk. Ranked by
    // edge so that when a weekend stacks (CFB + NFL + MMA + racing at once) the
    // best bets across every sport win the remaining room, rather than whichever
    // sport happens to be iterated first.
    picked.sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0));
    const globalSpent = { weekly: 0, futures: 0 };
    const out: RecommendedBetRow[] = [];
    for (const row of picked) {
      const pool = row.stakePool;
      if (globalSpent[pool] + row.suggestedStakeDollars > combined.globalCeilings[pool]) { cut++; continue; }
      globalSpent[pool] += row.suggestedStakeDollars;
      out.push(row);
    }
    return { rows: out.sort((a, b) => b.suggestedStakeDollars - a.suggestedStakeDollars), cut };
  }, [plans, combined, win]);
  const rows = windowed.rows.filter((r) => !isRowNotReady(r, readinessQuery.data));
  const cutByPool = windowed.cut;
  const unitDollars = settingsQuery.data?.unit_dollars ?? 0;

  const stats = useMemo(() => {
    const totalStake = rows.reduce((sum, r) => sum + r.suggestedStakeDollars, 0);
    const sports = new Set(rows.map((r) => r.sport)).size;
    const avgEdge = rows.length ? rows.reduce((sum, r) => sum + Math.abs(r.edge ?? 0), 0) / rows.length : null;
    return { count: rows.length, totalStake, sports, avgEdge };
  }, [rows]);

  const unitsLabel = (d: number) => (unitDollars > 0 ? ` (${(d / unitDollars).toFixed(1)}u)` : "");

  const futuresRows = futuresQuery.data ?? [];
  const futuresStats = useMemo(() => {
    const sports = new Set(futuresRows.map((r) => r.sport)).size;
    const avgEdge = futuresRows.length ? futuresRows.reduce((s, r) => s + Math.abs(r.edge ?? 0), 0) / futuresRows.length : null;
    return { count: futuresRows.length, sports, avgEdge };
  }, [futuresRows]);
  const isFutures = win === "futures";
  const loading = isFutures ? futuresQuery.isLoading : query.isLoading;

  async function handleMarkPlaced(row: RecommendedBetRow) {
    await markBetPlaced(row);
    queryClient.invalidateQueries({ queryKey: ["combined-recommended"] });
    queryClient.invalidateQueries({ queryKey: ["placed-bets", row.sport] });
    queryClient.invalidateQueries({ queryKey: ["placed-market-ids"] });
  }

  return (
    <PageShell title="All Recommended Bets">
      {query.isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}

      {loading ? (
        <>
          <StatTilesSkeleton />
          <TableSkeleton cols={8} />
        </>
      ) : (
        <>
          <div className="flex flex-wrap border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] mb-6 divide-x divide-[var(--color-border)]">
            {isFutures ? (
              <>
                <StatTile label="Futures shortlist" value={String(futuresStats.count)} sublabel={`top edges, real markets, across ${futuresStats.sports} sport${futuresStats.sports === 1 ? "" : "s"}`} />
                <StatTile label="Avg. edge" value={futuresStats.avgEdge !== null ? `${(futuresStats.avgEdge * 100).toFixed(1)}pp` : "—"} sublabel="model vs market" />
                <StatTile label="Tracking" value="settlement + calibration" sublabel="futures don't produce CLV (no single close)" />
              </>
            ) : (
              <>
                <StatTile label="Bets shown" value={String(stats.count)} sublabel={`across ${stats.sports} sport${stats.sports === 1 ? "" : "s"}`} />
                <StatTile
                  label="Total suggested stake"
                  value={`$${Math.round(stats.totalStake).toLocaleString()}${unitsLabel(stats.totalStake)}`}
                  sublabel="if every position were taken at once"
                />
                <StatTile
                  label="Avg. edge"
                  value={stats.avgEdge !== null ? `${(stats.avgEdge * 100).toFixed(1)}pp` : "—"}
                  sublabel="across bets shown"
                />
              </>
            )}
          </div>

          <div className="flex items-center gap-1.5 mb-3">
            {WINDOWS.map((w) => (
              <button
                key={w.key}
                onClick={() => setWin(w.key)}
                className={
                  win === w.key
                    ? "text-xs font-medium px-2.5 py-1 rounded-md bg-[var(--color-accent)] text-[#1c1408]"
                    : "text-xs font-medium px-2.5 py-1 rounded-md border border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)]"
                }
              >
                {w.label}
              </button>
            ))}
            <span className="ml-1 text-[11px] text-[var(--color-text-muted)]">
              {isFutures ? "season-long / tournament markets — place to track for calibration" : win === "all" ? "everything not yet started" : "games kicking off in this window"}
            </span>
          </div>
          {/* Each window sizes against the FULL pool, independently -- so working
              two windows in one sitting can double-commit without warning. */}
          {!isFutures && settingsQuery.data && <PoolExposure settings={settingsQuery.data} />}

          {isFutures ? (
            <CrossSportFuturesTable rows={futuresRows} />
          ) : (
            <>
              <RecommendedBetsTable rows={rows} onMarkPlaced={handleMarkPlaced} onShowReasoning={setReasoningRow} placedMarketIds={placedMarketIds} showSport />
              {/* Say out loud that the list is money-capped, not quality-capped.
                  Without this a bet that was 4th silently isn't there, which
                  reads as the app dropping it -- reported twice. */}
              {cutByPool > 0 && (
                <p className="mt-2 text-[11px] text-[var(--color-text-muted)]">
                  {cutByPool} more {cutByPool === 1 ? "bet qualifies" : "bets qualify"} in this window but
                  {" "}{cutByPool === 1 ? "was" : "were"} left out — each sport's pool is already fully
                  allocated above. Raise a sport's pool in Settings to see more of them.
                </p>
              )}
            </>
          )}
        </>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        {isFutures ? (
          <>
            A curated shortlist — the top futures by edge (model above market by ≥5pp) on markets that have
            actually traded, capped at a few per market type so no single ladder floods, and with the noisy
            player-stat / league-leader projections filtered out entirely. Futures don't produce CLV (no
            single closing line), so what you're building is a settlement + calibration record — did the
            model's futures predictions come true at their predicted rates? Marking one placed logs it in the
            tracker's Futures section (1 unit when the model didn't size a stake). Esports and some sim-based
            markets are still flagged "approx / tracking" — priced but not validated. For the full unfiltered
            list of any one sport, use that sport's own Futures page.
          </>
        ) : (
          <>
            Every sport's recommended bets in one place, each sized against its own bankroll slice (see Settings)
            and sorted by suggested stake. Same rules as the per-sport pages: quarter-Kelly, capped per position,
            3pp minimum edge, model_validated: false everywhere. Each sport's ceiling nets off the capital
            already committed to its pending bets, so a sport that is full stops suggesting until something
            settles. Weekly and futures are separate sub-pools and are tracked apart.
          </>
        )}
      </p>

      {reasoningRow && (
        <BetReasoningModal
          marketId={reasoningRow.marketId}
          modelProb={reasoningRow.estProb}
          marketProb={reasoningRow.impliedProb}
          sport={reasoningRow.sport}
          onClose={() => setReasoningRow(null)}
        />
      )}
    </PageShell>
  );
}
