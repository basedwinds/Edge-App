import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { RecommendedBetsTable } from "../components/markets/RecommendedBetsTable";
import { BetReasoningModal } from "../components/markets/BetReasoningModal";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import {
  fetchSettings,
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
} from "../api/markets";
import { CrossSportFuturesTable, type CrossSportFuturesRow } from "../components/markets/CrossSportFuturesTable";
import { fetchOpenBets, fetchSettledBets } from "../api/markets";
import type { FuturesMarketRow } from "../types/market";

// One place to see every sport's recommended bets at once. Each sport is built
// with its OWN pool sizes (from Settings) exactly as its dedicated page does;
// rows are then merged and sorted by suggested stake. Locked-pool subtraction
// is skipped here (this is a read-only overview) -- mark bets from here or from
// the per-sport page, both route by row.sport.
//
// Every fetch is timeout-guarded (a single heavy endpoint -- e.g. NFL's
// /markets, which can return tens of thousands of rows against a bloated
// snapshot table -- must not hang or block the whole combined view). A fetch
// that errors or exceeds the budget contributes nothing rather than failing
// the page; the sports that DID load still render.
function guard<T>(p: Promise<T>, fallback: T, ms = 18000): Promise<T> {
  return Promise.race([
    p.catch(() => fallback),
    new Promise<T>((res) => setTimeout(() => res(fallback), ms)),
  ]);
}

async function loadCombined(): Promise<RecommendedBetRow[]> {
  const s = await fetchSettings();
  // Game/match markets only -- this is an "upcoming bets" view, and the
  // per-sport /futures endpoints are both the slowest (season-long models +
  // depth-chart lookups) and season-long, not "upcoming". They stay on each
  // sport's own Futures page. `[]` is passed for the futures arg below.
  const [
    nflM, nbaM, wnbaM, cfbM, mlbM, mmaM, tenM, socM, valM, cs2M, lolM, racingM,
  ] = await Promise.all([
    guard(fetchMarkets(), []), guard(fetchNbaMarkets(), []),
    guard(fetchWnbaMarkets(), []), guard(fetchCfbMarkets(), []),
    guard(fetchMlbMarkets(), []), guard(fetchMmaMarkets(), []),
    guard(fetchTennisMarkets(), []), guard(fetchSoccerMarkets(), []), guard(fetchValorantMarkets(), []),
    guard(fetchCs2Markets(), []), guard(fetchLolMarkets(), []), guard(fetchRacingMarkets(), []),
  ]);
  const rows = [
    ...buildRecommendedBets(nflM, [], s.weekly_pool_dollars, s.futures_pool_dollars).rows,
    ...buildNbaRecommendedBets(nbaM, [], s.nba_weekly_pool_dollars, s.nba_futures_pool_dollars).rows,
    ...buildWnbaRecommendedBets(wnbaM, s.wnba_weekly_pool_dollars, s.wnba_futures_pool_dollars).rows,
    ...buildCfbRecommendedBets(cfbM, s.cfb_weekly_pool_dollars, s.cfb_futures_pool_dollars).rows,
    ...buildMlbRecommendedBets(mlbM, [], s.mlb_weekly_pool_dollars, s.mlb_futures_pool_dollars).rows,
    ...buildMmaRecommendedBets(mmaM, s.mma_weekly_pool_dollars).rows,
    ...buildTennisRecommendedBets(tenM, s.tennis_weekly_pool_dollars).rows,
    ...buildSoccerRecommendedBets(socM, s.soccer_weekly_pool_dollars).rows,
    ...buildValorantRecommendedBets(valM, s.valorant_weekly_pool_dollars, s.valorant_futures_pool_dollars).rows,
    ...buildCs2RecommendedBets(cs2M, s.cs2_weekly_pool_dollars, s.cs2_futures_pool_dollars).rows,
    ...buildLolRecommendedBets(lolM, s.lol_weekly_pool_dollars, s.lol_futures_pool_dollars).rows,
    ...buildRacingRecommendedBets(racingM, s.racing_weekly_pool_dollars).rows,
  ];
  rows.sort((a, b) => b.suggestedStakeDollars - a.suggestedStakeDollars);
  return rows;
}

// Cross-sport futures for the "Futures" window: every sport's /futures merged,
// tagged with sport, filtered to edge-qualified (>=3pp model-vs-market gap),
// sorted by edge. WNBA/MMA have no futures, so they're omitted.
async function loadCombinedFutures(): Promise<CrossSportFuturesRow[]> {
  const [nfl, nba, mlb, ten, soc, val, cs2, lol] = await Promise.all([
    guard(fetchFutures(), []), guard(fetchNbaFutures(), []), guard(fetchMlbFutures(), []),
    guard(fetchTennisFutures(), []), guard(fetchSoccerFutures(), []), guard(fetchValorantFutures(), []),
    guard(fetchCs2Futures(), []), guard(fetchLolFutures(), []),
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
    const key = `${r.sport}|${r.market_type}|${r.team ?? ""}|${r.side ?? ""}|${r.line ?? ""}`;
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

const EMPTY_IDS: Set<string> = new Set(); // stable ref for the loading state (cross-platform placed keys)

function localDateStr(offsetDays = 0): string {
  return new Date(Date.now() + offsetDays * 86400000).toLocaleDateString("en-CA"); // YYYY-MM-DD, local
}

type WindowFilter = "today" | "2d" | "all" | "futures";
const WINDOWS: { key: WindowFilter; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "2d", label: "Next 2 days" },
  { key: "all", label: "All upcoming" },
  { key: "futures", label: "Futures" },
];

export function Combined() {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["combined-recommended"], queryFn: loadCombined });
  const futuresQuery = useQuery({ queryKey: ["combined-futures"], queryFn: loadCombinedFutures });
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
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

  const allRows = query.data ?? [];
  const rows = useMemo(() => {
    // Bucket by the REAL start instant. Match sports (tennis/soccer/esports/MMA)
    // carry an accurate estimatedStartTime -- and a `gameday` derived from a
    // STALE match_date (tennis showed match_date 5 days before the real start),
    // so filtering on gameday silently dropped those bets from Today/Next-2-days
    // AND couldn't tell a game had already started (a 7am kickoff still showed
    // "today"). Prefer the timestamp; fall back to gameday's date only when no
    // timestamp exists (team sports, where gameday IS accurate). Reported
    // 2026-07-24.
    const now = Date.now();
    const today = localDateStr(0);
    const limit = win === "today" ? today : localDateStr(1); // "2d" = today + tomorrow
    const start = (r: RecommendedBetRow): { ms: number | null; date: string | null } => {
      if (r.estimatedStartTime) {
        const ms = Date.parse(r.estimatedStartTime);
        if (!Number.isNaN(ms)) return { ms, date: new Date(ms).toLocaleDateString("en-CA") };
      }
      return { ms: null, date: r.gameday };
    };
    return allRows.filter((r) => {
      const { ms, date } = start(r);
      if (ms !== null && ms < now) return false;        // already started -> not "upcoming"
      if (win === "all") return true;                    // futures (date null) live only here
      if (date === null) return false;                   // season-long futures: All-upcoming only
      return date >= today && date <= limit;             // kicks off within the window
    });
  }, [allRows, win]);
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

          {isFutures ? (
            <CrossSportFuturesTable rows={futuresRows} />
          ) : (
            <RecommendedBetsTable rows={rows} onMarkPlaced={handleMarkPlaced} onShowReasoning={setReasoningRow} placedMarketIds={placedMarketIds} showSport />
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
            3pp minimum edge, model_validated: false everywhere. This is a read-only overview — it doesn't
            subtract capital already locked in pending bets, so cross-check the per-sport page before sizing up.
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
