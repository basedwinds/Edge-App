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
  fetchCodMarkets, buildCodRecommendedBets,
  fetchRacingMarkets, buildRacingRecommendedBets,
  markBetPlaced,
  fetchFutures, fetchNbaFutures, fetchMlbFutures, fetchTennisFutures,
  fetchSoccerFutures, fetchValorantFutures, fetchCs2Futures, fetchLolFutures,
  fetchWnbaFutures, fetchCfbFutures, fetchRacingFutures, fetchMmaFutures,
  type RecommendedBetRow,
  type SettingsPayload,
} from "../api/markets";
import { crossPlatformKey } from "../api/markets";
import { gameIdForRow } from "../lib/sports";
import { CrossSportFuturesTable, type CrossSportFuturesRow } from "../components/markets/CrossSportFuturesTable";
import { LADDER_TYPES } from "../utils/futuresGroupCap";
import { fetchReadiness, isRowNotReady, isFuturesSportNotReady } from "../api/markets";
import { fetchOpenBets } from "../api/markets";
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
// PERSISTED ACROSS PAGE LOADS, which is the half of this the in-memory Map
// could never cover.
//
// The 2026-08-04 fix above stopped a timeout rendering as "no bets" -- but only
// once the map had something in it, i.e. from the SECOND poll of a session
// onwards. A Map is empty on every fresh load, so the very first render after
// opening or refreshing the app still fell back to [] for any sport slower than
// the 18s budget, and the sport vanished until the next poll warmed it. Measured
// cold on 2026-08-12: /soccer/markets 110s, /cs2 176s, /tennis 178s, /markets
// 90s -- so on a cold backend that is most of the board, every time.
//
// User-reported twice now, and the second report is what identified the gap:
// "3 Leagues Cup matches for today just disappeared", "last night MLB games kept
// showing up and disappearing". Same mechanism both times.
//
// sessionStorage, not localStorage: this is a within-session staleness cushion,
// not a cache to resurrect a day-old board. Writes are size-capped and wrapped
// -- NFL's /markets alone can be tens of thousands of rows, and a QuotaExceeded
// on one feed must not take down the page or block the others.
// ONLY STAKED ROWS ARE PERSISTED, and that is what makes this fit at all.
//
// First attempt stored whole payloads and was worse than useless: the store hit
// 5.5MB (over the ~5MB sessionStorage quota, so writes silently failed) and the
// 2MB-per-entry cap it used skipped exactly the biggest feeds -- SoccerMarkets,
// 9,612 rows, was ABSENT. The sport that flickers most was the one not covered.
//
// Every builder on this page drops rows without a suggested stake as its first
// act (`if (m.suggested_stake_dollars === null) continue`), and the futures
// shortlist filters on the same field. So a row with no stake can never become
// a recommendation, and keeping it in the cushion buys nothing. Soccer goes
// 9,612 rows -> ~57.
const LAST_GOOD_KEY = "combined:lastGood";

function readPersistedLastGood(): Map<string, unknown> {
  try {
    const raw = sessionStorage.getItem(LAST_GOOD_KEY);
    if (raw) return new Map(Object.entries(JSON.parse(raw) as Record<string, unknown>));
  } catch {
    /* unreadable or disabled storage just means no cushion, never a broken page */
  }
  return new Map<string, unknown>();
}

const lastGood = readPersistedLastGood();

/** The cushion keeps only rows that could ever be recommended -- see the note on
 * LAST_GOOD_KEY. Non-arrays (settings, open bets) pass through untouched. */
function trimForPersist(value: unknown): unknown {
  if (!Array.isArray(value)) return value;
  return value.filter(
    (r) => r && typeof r === "object" && (r as { suggested_stake_dollars?: number | null }).suggested_stake_dollars != null,
  );
}

function persistLastGood(): void {
  try {
    const obj: Record<string, unknown> = {};
    for (const [k, v] of lastGood) obj[k] = trimForPersist(v);
    sessionStorage.setItem(LAST_GOOD_KEY, JSON.stringify(obj));
  } catch {
    /* over quota / disabled -- degrade to the old in-memory-only behaviour */
  }
}

/** Guarded fetches that fell back on THIS load -- a timeout or an error, not an
 * empty board. Read by the page to say so out loud.
 *
 * WHY THIS EXISTS. guard() times out at 18s and silently substitutes [] (or the
 * last good value). Several routes legitimately take far longer than that on a
 * cold cache -- /soccer/markets and /cs2/markets were measured at 110s and 176s
 * right after a backend restart -- so the sport contributed NOTHING and the page
 * still said "across 6 sports" as if that were the whole board.
 *
 * Measured 2026-08-12: the futures shortlist should have been 29 rows across 8
 * sports and rendered 13 across 6. The 16 missing rows were exactly CFB (8) and
 * soccer (8) -- and CFB held the #2, #4, #5 and #6 largest edges on the board,
 * including a +51.1pp win total. A user cannot tell "this sport has no bets"
 * from "this sport did not load", and the second one silently hides the best
 * bets available.
 *
 * TWO SETS, ONE PER LOADER, and not one shared set cleared at the top of each.
 * loadCombined and loadCombinedFutures are separate useQuery calls that run
 * CONCURRENTLY, so a single module-level set cleared by both means whichever
 * starts second wipes the other's record and the banner under-reports exactly
 * when the board is worst. */
export const degradedGameKeys = new Set<string>();
export const degradedFuturesKeys = new Set<string>();

function guard<T>(key: string, p: Promise<T>, fallback: T, ms = 18000,
                  into: Set<string> = degradedGameKeys): Promise<T> {
  const remembered = () => {
    into.add(key);
    // A REMEMBERED VALUE IS STILL DEGRADED. It is stale by definition -- the
    // point of saying so is that the counts below are not a live read.
    return lastGood.has(key) ? (lastGood.get(key) as T) : fallback;
  };
  return Promise.race([
    p.then((v) => {
      lastGood.set(key, v);
      persistLastGood();   // survive a page refresh -- see LAST_GOOD_KEY
      return v;
    }).catch(remembered),
    new Promise<T>((res) => setTimeout(() => res(remembered()), ms)),
  ]);
}

/** guard() for the futures loader, so its fallbacks are recorded separately. */
function guardF<T>(key: string, p: Promise<T>, fallback: T, ms = 18000): Promise<T> {
  return guard(key, p, fallback, ms, degradedFuturesKeys);
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
 * around turnover.
 *
 * THIS IS NOT THE ONLY BANKROLL CAP, AND THE TWO DO NOT MATCH ON PURPOSE.
 * backend models/exposure.py caps OUTSTANDING real exposure at 40% game /
 * 20% futures (60% total). This caps the same quantity -- it subtracts
 * `liveWeekly`/`liveFutures` before allowing new rows -- but at 30% / 10%.
 *
 * So there are two independent ceilings on one number and THE STRICTER ONE
 * BINDS, which is always this file: a slate can never be funded past 30% even
 * though exposure.py would permit 40%. That ordering is deliberate (a
 * recommendation list should be tighter than the hard safety stop behind it),
 * but it means exposure.py's 40/20 is effectively unreachable through this
 * page, and anyone reading only one of the two files will quote a number the
 * app never actually enforces. Measured 2026-08-11: $170 outstanding against
 * this file's $600, so neither is close to binding today.
 *
 * If you change the appetite, change BOTH or the looser one silently does
 * nothing. */
const GLOBAL_CAP_PCT = { weekly: 0.30, futures: 0.10 } as const;

type CombinedPlan = { plans: SportPlan[]; globalCeilings: Record<"weekly" | "futures", number> };

async function loadCombined(): Promise<CombinedPlan> {
  degradedGameKeys.clear();   // this load's fallbacks only -- see guard()
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
    codM,
  ] = await Promise.all([
    guard("Markets", fetchMarkets(), []), guard("NbaMarkets", fetchNbaMarkets(), []),
    guard("WnbaMarkets", fetchWnbaMarkets(), []), guard("CfbMarkets", fetchCfbMarkets(), []),
    guard("MlbMarkets", fetchMlbMarkets(), []), guard("MmaMarkets", fetchMmaMarkets(), []),
    guard("TennisMarkets", fetchTennisMarkets(), []), guard("SoccerMarkets", fetchSoccerMarkets(), []), guard("ValorantMarkets", fetchValorantMarkets(), []),
    guard("Cs2Markets", fetchCs2Markets(), []), guard("LolMarkets", fetchLolMarkets(), []), guard("RacingMarkets", fetchRacingMarkets(), []),
    guard("CodMarkets", fetchCodMarkets(), []),  // CoD was absent -- see plan("cod")
  ]);
  // Keys of everything already placed, so a bet you have ALREADY TAKEN cannot be
  // offered again.
  //
  // It was being counted TWICE against its sport. Its stake is subtracted from
  // the ceiling below (`locked`), and the same market also stayed in `ranked`
  // and consumed the ceiling a second time -- so placing one $10 bet burned $20
  // of a $60 sport budget, which is why marking a bet made OTHER bets vanish.
  // Measured on the live board: 14 of 18 open bets were simultaneously locking
  // capital and still being offered, $140 double-counted.
  //
  // Same key the "Placed" badge uses (cross_key + game key), so a proposition
  // placed on Kalshi is also recognised on its Polymarket twin.
  const placedKeys = new Set<string>();
  for (const b of openBets) {
    placedKeys.add(b.cross_key);
    if (b.game_key) placedKeys.add(`game:${b.game_key}`);
  }
  const notPlaced = (r: RecommendedBetRow) => {
    if (placedKeys.has(crossPlatformKey(r))) return false;
    const gid = gameIdForRow(r);
    return !(gid && placedKeys.has(`game:${gid}`));
  };

  const plan = (sport: string, ranked: RecommendedBetRow[], weeklyPool: number, futuresPool = 0): SportPlan => {
    const l = locked.get(sport) ?? { weekly: 0, futures: 0 };
    return {
      ranked: ranked.filter(notPlaced),
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
    // CoD was never fetched here, so its bets could not appear on the
    // cross-sport board no matter what edge they carried -- the same
    // hand-maintained per-sport-list drift this file already fixed twice
    // (WNBA/CFB futures, then racing/MMA futures). It has its own
    // /cod/markets route, builder and Recommended page; only this list was
    // missing it. Currently contributes nothing because its tournament has
    // ended (all 25 rows closed/finalized), which is exactly why the gap
    // went unnoticed -- it will matter the next time CoD has live matches.
    plan("cod", buildCodRecommendedBets(codM, s.cod_weekly_pool_dollars, s.cod_futures_pool_dollars).ranked, s.cod_weekly_pool_dollars, s.cod_futures_pool_dollars),
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
  degradedFuturesKeys.clear();   // this load's fallbacks only -- see guard()
  // Every sport with a /futures route must be listed here. WNBA and CFB were
  // missing -- their backend routes existed and returned real edge-qualified
  // rows (CFB 139, the most of any sport; WNBA 12), but no fetcher ever called
  // them, so neither could appear on this tab at all. That is not "crowded out
  // by a bigger sport", it is absent, and no per-sport cap can fix absence.
  const [nfl, nba, wnba, cfb, mlb, ten, soc, val, cs2, lol, racing, mma] = await Promise.all([
    guardF("Futures", fetchFutures(), []), guardF("NbaFutures", fetchNbaFutures(), []),
    guardF("WnbaFutures", fetchWnbaFutures(), []), guardF("CfbFutures", fetchCfbFutures(), []),
    guardF("MlbFutures", fetchMlbFutures(), []),
    guardF("TennisFutures", fetchTennisFutures(), []), guardF("SoccerFutures", fetchSoccerFutures(), []), guardF("ValorantFutures", fetchValorantFutures(), []),
    guardF("Cs2Futures", fetchCs2Futures(), []), guardF("LolFutures", fetchLolFutures(), []),
    // Racing and MMA were absent from this list, so their futures could never
    // appear here no matter what edge they carried -- the same hand-maintained
    // per-sport-list drift that hid whole sports from the catalog scanner.
    guardF("RacingFutures", fetchRacingFutures(), []), guardF("MmaFutures", fetchMmaFutures(), []),
  ]);
  const tag = (fr: FuturesMarketRow[], sport: CrossSportFuturesRow["sport"]) =>
    fr.map((r) => ({ ...r, sport }));
  const all: CrossSportFuturesRow[] = [
    ...tag(nfl, "nfl"), ...tag(nba, "nba"), ...tag(wnba, "wnba"), ...tag(cfb, "cfb"),
    ...tag(mlb, "mlb"), ...tag(ten, "tennis"),
    ...tag(soc, "soccer"), ...tag(val, "valorant"), ...tag(cs2, "cs2"), ...tag(lol, "lol"),
    ...tag(mma, "mma"),
    // Racing's single /futures route covers three series, so each row carries
    // its own `sport` (f1 | irl | nascar). Tagging them all "f1" would send the
    // reasoning link and the placed-bet key to the wrong series.
    ...racing.map((r) => ({ ...r, sport: (r.sport ?? "f1") as CrossSportFuturesRow["sport"] })),
  ];
  // Curated to a number you can actually keep up with. Raw positive-edge is
  // ~800+ and mostly noise, so:
  //  1. drop the phantom player-stat/leader markets (naive last-season-rate
  //     projections that over-project -- the app already flags them tracking-only),
  //  2. require the market to have actually TRADED (a seeded price isn't bettable),
  //  3. POSITIVE edge >= 5pp (real disagreement, clears fee noise),
  //  4. dedup the same proposition across Kalshi/Polymarket (keep the bigger edge),
  //  5. cap to the top 3 per (sport, market_type) so no single ladder floods,
  //  6. cap to the top 8 per SPORT.
  //
  // REPRESENTATION IS GUARANTEED, and it matters that it is. Steps 5 and 6 are
  // both keyed per-sport and both `continue` (skip one row) rather than `break`
  // (stop the scan), and there is NO global row limit -- so the loop always
  // reaches every sport, and a sport with one qualifying future keeps it no
  // matter how many higher-edge rows another sport brings.
  //
  // Verified adversarially: injecting 5,000 synthetic MLB futures at a 90pp
  // edge -- far past anything real -- changed no other sport's slot count, and
  // tennis (weakest, 1 row) kept its row. Every gate above is either a property
  // of the row itself (1-3) or scoped within one sport (4-6); none of them let
  // sports compete against each other.
  //
  // If a global dollar cap is ever added here, it MUST be two-pass like the
  // game side (per-sport ceiling first, then global) -- spending one pool in
  // edge order is exactly what would break this.
  // NOTE: placed futures are NOT excluded here. The table itself marks them
  // "Placed ✓" (matched by real-world proposition, so both a Kalshi and a
  // Polymarket copy of the same future count) -- excluding by market_id used to
  // hide the Kalshi copy while still showing the Polymarket one, so a future you
  // HAD placed kept re-appearing as unplaced. Showing the full list with a
  // placed badge is clearer and stops duplicate placements.
  const isPhantom = (mt: string) => mt.startsWith("season_") || mt.startsWith("leader_");
  // ONLY futures the model actually SIZED reach this tab. This is the
  // cross-sport "what should I place" list, so a row it declined to stake does
  // not belong in it, whatever its headline edge.
  //
  // The tab used to list anything from 5pp up while the app only BETS from 10pp,
  // and the table filled the gap with a fabricated 0.25u -- so 243 of 379 rows
  // read as recommendations that no gate had ever approved. Breakdown of those
  // 243: 165 under the 10pp betting gate, 67 stopped by another gate (CLV
  // bucket, untraded, kelly<=0), and 11 outright blocked as implausible (the
  // reported "Freecs to win LCK 2026" at a 28x model-vs-market disagreement).
  //
  // Testing suggested_stake_dollars rather than re-checking the edge is
  // deliberate: it is the single output every one of those gates already feeds
  // into, so this cannot drift out of step with them the way a duplicated
  // threshold would. The per-sport Futures pages still show the full list --
  // that is where tracking and calibration live.
  const candidates = all
    .filter((r) => r.edge !== null && r.edge >= 0.05 && r.volume && !isPhantom(r.market_type)
                   && r.suggested_stake_dollars != null)
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
      // SETTLED bets are deliberately NOT included.
      //
      // A settled bet's own market is closed -- the event finished. So a LIVE
      // row sharing its cross_key can only be a DIFFERENT event, and counting
      // settled bets here produces false "placed" badges and nothing else.
      // Measured: cross_key for a tournament future is sport|market_type|team
      // with no tournament in it, so Alexander Bublik losing ATP Kitzbuhel
      // marked Bublik as already-placed in EVERY later tennis tournament, and
      // three CS2 teams from BLAST Bounty did the same. Reported as "marked
      // placed in futures but not in the Bet Tracker", which is exactly right:
      // the tracker had nothing open, the badge was reading a finished bet.
      const open = await fetchOpenBets();
      // cross-platform proposition keys + game-level keys (see usePlacedKeys)
      const keys = new Set<string>();
      for (const b of open) {
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
    // Is this bet still placeable at all? This is what the ALLOCATION uses, so
    // the funded set never depends on which tab is open.
    const isUpcoming = (r: RecommendedBetRow) => {
      if (r.stakePool === "futures") return false;   // season-long -> Futures tab
      const { ms, date } = start(r);
      if (ms !== null) return ms >= now;             // already started -> not placeable
      return date !== null;                          // no clock: keep, the day-level test below filters it
    };

    // Is it inside the selected view? Applied to the funded set only.
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

    // ALLOCATE ONCE OVER THE WHOLE UPCOMING SLATE, THEN FILTER BY WINDOW.
    //
    // This used to run the other way: the window was applied first and each
    // sport's ceiling was spent on whatever survived, so every window answered
    // "the best use of this pool on THIS slate". That reads reasonable and is
    // actively harmful, because a narrow window hides the competition rather
    // than losing to it. Measured on the live board: Valorant's six slots go to
    // Enterprise +61.0pp, Eintracht +56.5pp, Joblife +48.3pp and REBORN +43.0pp,
    // all starting Aug 6-7. Under "Next 24h" those are invisible, so the same
    // pool funded GiantX GC at +14.1pp -- the app told you to place a 14pp bet
    // while your Valorant money was better spent on a 61pp one two days out.
    //
    // It also made the windows contradict each other: a bet funded under "Next
    // 24h" simply vanished under "All upcoming". Reported three times (Dplus
    // KIA, New Meta vs Fennel, G2 Gozen vs GiantX GC).
    //
    // So the allocation is now one decision over every upcoming bet, and the
    // windows are pure views on it -- which is what they are for. "All upcoming"
    // is a true superset of "Next 48h" is a true superset of "Next 24h", and a
    // row means the same thing in all three.
    //
    // The old comment warned this makes narrow windows sparse. It does, and that
    // is the honest answer: if the best use of a pool starts on Thursday, the
    // right number of bets to place today is small. Sparse-but-correct beats
    // full-of-second-best.
    //
    // PASS 1 -- each sport spends its OWN ceiling. This is what guarantees every
    // sport with real candidates is represented at all: without it a couple of
    // high-edge sports absorb the whole budget (measured on the live board: the
    // top 20 by raw edge contained zero MLB despite 21 qualifying candidates).
    let cut = 0;                 // qualified and upcoming, but a ceiling was full
    const picked: RecommendedBetRow[] = [];
    for (const p of plans) {
      const spent = { weekly: 0, futures: 0 };
      for (const row of p.ranked) {
        if (!isUpcoming(row)) continue;
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
    // The window now filters the FUNDED set rather than feeding the allocation,
    // so switching tabs can only ever remove rows, never change what a row says.
    const funded = out.sort((a, b) => b.suggestedStakeDollars - a.suggestedStakeDollars);
    return { rows: funded.filter(inWindow), funded, cut };
  }, [plans, combined, win]);
  const rows = windowed.rows.filter((r) => !isRowNotReady(r, readinessQuery.data));
  const cutByPool = windowed.cut;
  // Funded bets that start beyond the selected window. Distinct from cutByPool:
  // these ARE bets to place, just not yet -- the narrow view is short by timing,
  // not by budget.
  //
  // Counted AFTER the readiness filter, on both sides. Reading it off the raw
  // memo said "24 more" while "All upcoming" listed 14 more, because
  // out-of-season sports are dropped here rather than in the allocation.
  const laterCount = Math.max(
    0,
    windowed.funded.filter((r) => !isRowNotReady(r, readinessQuery.data)).length - rows.length,
  );
  // Which sports fell back on this load (see guard / the two degraded*Keys sets).
  // Derived from
  // the guard KEY so it cannot drift from the fetch list: every key is
  // "<Sport>Markets" or "<Sport>Futures", and the bare "Markets"/"Futures" pair
  // is NFL. Recomputed whenever either query settles.
  const degradedSports = useMemo(() => {
    const pretty: Record<string, string> = {
      "": "NFL", Nba: "NBA", Wnba: "WNBA", Cfb: "College Football", Mlb: "MLB",
      Mma: "MMA", Tennis: "Tennis", Soccer: "Soccer", Valorant: "Valorant",
      Cs2: "CS2", Lol: "LoL", Racing: "Racing",
    };
    const names = new Set<string>();
    for (const key of [...degradedGameKeys, ...degradedFuturesKeys]) {
      const stem = key.replace(/(Markets|Futures)$/, "");
      if (stem === key) continue;            // e.g. OpenBetsForPool -- not a sport
      const name = pretty[stem];
      if (name) names.add(name);
    }
    return [...names].sort();
  }, [query.data, futuresQuery.data]);

  const unitDollars = settingsQuery.data?.unit_dollars ?? 0;

  const stats = useMemo(() => {
    const totalStake = rows.reduce((sum, r) => sum + r.suggestedStakeDollars, 0);
    const sports = new Set(rows.map((r) => r.sport)).size;
    const avgEdge = rows.length ? rows.reduce((sum, r) => sum + Math.abs(r.edge ?? 0), 0) / rows.length : null;
    return { count: rows.length, totalStake, sports, avgEdge };
  }, [rows]);

  const unitsLabel = (d: number) => (unitDollars > 0 ? ` (${(d / unitDollars).toFixed(1)}u)` : "");

  // Gate out-of-season sports HERE, not only inside the table, so the summary
  // tile counts the same rows the table shows. The tile used to read the raw
  // list and the table filtered internally, so an out-of-season sport inflated
  // the headline: "45 across 9 sports" above a table showing 29 across 7. The
  // gap was small until NFL and CFB futures (114 and 139 candidates, both
  // pre-season) started arriving, which made it obvious.
  const futuresRows = useMemo(() => {
    const ready = (futuresQuery.data ?? []).filter((r) => !isFuturesSportNotReady(r.sport, readinessQuery.data));
    const s = settingsQuery.data;
    if (!s) return ready;   // fail OPEN: never hide a real bet because settings are still loading

    // DOLLAR CAPS, two-pass -- the row caps in loadCombinedFutures bound the
    // COUNT per sport but nothing bounded the total spend. With 11 sports x 8
    // rows x 0.25u that reaches $220 against a $200 global futures ceiling
    // (10% of bankroll), so the tab could out-commit the cap it is supposed to
    // sit inside. Not binding at today's volume, but only because most sports
    // are out of season at once.
    //
    // Two-pass is REQUIRED, and the comment in loadCombinedFutures says so:
    // spending a single pool in edge order lets one sport deep in its season
    // consume the room before another sport is ever reached. Pass 1 gives each
    // sport its own ceiling; pass 2 ranks the survivors by edge for the shared
    // remainder, so the best bets across every sport win what is left.
    // PASS 1 is already done, in loadCombinedFutures: the per-sport row cap
    // (MAX_FUTURES_PER_SPORT, applied with `continue` not `break`) is this
    // list's per-sport ceiling, and it is what guarantees a sport with one
    // qualifying future keeps it no matter how many higher-edge rows another
    // sport brings. So this is PASS 2 -- the global cap only.
    //
    // A per-sport DOLLAR ceiling was tried here and removed: at
    // PORTFOLIO_CEILING_PCT (0.6) of a $28.16 futures pool it is $16.90, which
    // at 0.25u would cut soccer and MLB from 8 rows to 6 -- a tighter and
    // different constraint than the row cap that was asked for, and one that
    // would silently shrink the list. The row cap already keeps every sport
    // under its pool: 8 rows x 0.25u = $20 against $28.16.
    const sorted = [...ready].sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0));
    let globalSpent = 0;
    const out: CrossSportFuturesRow[] = [];
    for (const r of sorted) {
      // Every row is charged, including one already marked placed (those stay
      // in this list by design, badged "Placed ✓"). globalCeilings.futures has
      // ALREADY netted off committed capital, so such a row is counted twice.
      //
      // That is deliberate. Excluding them needs a placed-row test, and the
      // set available here (placedMarketIds) holds cross_keys, not the
      // proposition keys this table matches futures on -- a near-miss key
      // format would silently under-count and let the cap be exceeded, which is
      // the one failure this exists to prevent. Double-counting can only show
      // one row too FEW, and only once the cap is near binding; today it sits
      // at $52.50 against $200.
      const stake = r.suggested_stake_dollars ?? 0;
      if (globalSpent + stake > combined.globalCeilings.futures) continue;
      globalSpent += stake;
      out.push(r);
    }
    return out;
  }, [futuresQuery.data, readinessQuery.data, settingsQuery.data, combined.globalCeilings.futures]);
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
          {degradedSports.length > 0 && (
            <div className="mb-4 rounded-lg border border-[var(--color-warning)] bg-[var(--color-surface)] px-3 py-2 text-xs text-[var(--color-warning)]">
              <span className="font-semibold">Incomplete board:</span>{" "}
              {degradedSports.join(", ")} did not load in time, so {degradedSports.length === 1 ? "its" : "their"}{" "}
              bets are missing from the counts below. This is a slow or restarting backend, not an
              empty slate — hit Refresh in a moment. (Measured 2026-08-12: a cold
              backend hid 16 rows, including the four largest edges on the board.)
            </div>
          )}

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
          {/* Allocation happens once over the whole upcoming slate, so the pool is
              committed once no matter how many windows you look at. */}
          {!isFutures && settingsQuery.data && <PoolExposure settings={settingsQuery.data} />}

          {isFutures ? (
            <CrossSportFuturesTable rows={futuresRows} />
          ) : (
            <>
              <RecommendedBetsTable rows={rows} onMarkPlaced={handleMarkPlaced} onShowReasoning={setReasoningRow} placedMarketIds={placedMarketIds} showSport />
              {/* Two different reasons a bet you expected is absent, kept apart:
                  it starts outside this view, or the pool had no room for it. */}
              {laterCount > 0 && (
                <p className="mt-2 text-[11px] text-[var(--color-text-muted)]">
                  {laterCount} more funded {laterCount === 1 ? "bet starts" : "bets start"} after this window —
                  see &ldquo;All upcoming&rdquo;.
                </p>
              )}
              {cutByPool > 0 && (
                <p className="mt-2 text-[11px] text-[var(--color-text-muted)]">
                  {cutByPool} more {cutByPool === 1 ? "bet qualifies" : "bets qualify"} but
                  {" "}{cutByPool === 1 ? "has" : "have"} no room — the pool is fully allocated to higher
                  edges. Raise a sport&rsquo;s pool in Settings to fund more.
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
