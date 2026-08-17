import { useMemo, useState } from "react";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpDown, CircleCheck, Hourglass, Info, Loader2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { perGamePoolLabel, fetchOpenBets, fetchReadiness, isRowNotReady, crossPlatformKey, rowGameId, type RecommendedBetRow } from "../../api/markets";

// Shared across every Recommended page: the cross-platform KEYS of bets already
// placed (any pool, EITHER book), so a proposition you've marked reads "Placed"
// even when the deduped row flips to show the other platform's copy -- otherwise
// the same real bet re-appears as unplaced and gets placed twice. `cross_key`
// comes from the backend and is byte-identical to this file's crossPlatformKey.
// (Query key name kept for cache-sharing + invalidation; it now holds keys.)
// Two keys per placed bet: its exact proposition (cross_key, platform-agnostic)
// AND its game (game_key, "" for futures). Matching on EITHER means a game you've
// already bet reads "Placed" even when the single row shown for it flips to a
// different line -- consistent with the one-row-per-game cap the list applies.
function usePlacedKeys(enabled: boolean): Set<string> {
  const q = useQuery({
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
      const keys = new Set<string>();
      for (const b of open) {
        keys.add(b.cross_key);
        if (b.game_key) keys.add(`game:${b.game_key}`);
      }
      return keys;
    },
    enabled,
  });
  return q.data ?? EMPTY_PLACED;
}
const EMPTY_PLACED: Set<string> = new Set();
import { SourceBadge } from "./SourceBadge";
import { EdgeBadge } from "./EdgeBadge";
import type { SportKey } from "../../lib/sports";
import { describePick, marketTypeLabel } from "../../utils/pickLabel";


function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

const columnHelper = createColumnHelper<RecommendedBetRow>();

// All 30 MLB teams' real, stable IANA timezone -- ported from
// backend/app/data/mlb_ballparks.py::TEAM_TZ (same source, same values) for
// the day-boundary fix below. Roof type doesn't affect a team's timezone,
// so this covers every team, unlike the weather-specific 21-team subset.
const MLB_TEAM_TZ: Record<string, string> = {
  ATL: "America/New_York", BAL: "America/New_York", BOS: "America/New_York",
  CHC: "America/Chicago", CIN: "America/New_York", CLE: "America/New_York",
  COL: "America/Denver", CWS: "America/Chicago", DET: "America/New_York",
  KC: "America/Chicago", LAA: "America/Los_Angeles", LAD: "America/Los_Angeles",
  MIN: "America/Chicago", NYM: "America/New_York", NYY: "America/New_York",
  PHI: "America/New_York", PIT: "America/New_York", SD: "America/Los_Angeles",
  SF: "America/Los_Angeles", STL: "America/Chicago", WSH: "America/New_York",
  AZ: "America/Phoenix", HOU: "America/Chicago", MIA: "America/New_York",
  MIL: "America/Chicago", SEA: "America/Los_Angeles", TB: "America/New_York",
  TEX: "America/Chicago", TOR: "America/Toronto", ATH: "America/Los_Angeles",
};

// REAL BUG fixed here (2026-07-17), same root cause and same fix as
// backend/app/models/clv.py::_mlb_kickoff_utc and
// backend/app/api/routers/mlb_markets.py::_game_kickoff_local: MLB's
// `gametime` is a raw UTC clock reading with no date, and naively pairing it
// with `gameday` (the LOCAL calendar date) silently assumes the UTC
// calendar day equals the local one -- FALSE for evening games at negative
// UTC offsets (the real instant is on gameday+1). Resolves it the same way:
// try both candidate UTC days, keep whichever one's local conversion (at
// this game's home team's real timezone) round-trips back to `gameday`.
function mlbKickoffInstant(gameday: string, gametime: string, homeTeam: string): Date | null {
  const tz = MLB_TEAM_TZ[homeTeam];
  if (!tz) return null;
  const fmt = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" });
  for (const dayOffset of [0, 1]) {
    const base = new Date(gameday + "T00:00:00Z");
    base.setUTCDate(base.getUTCDate() + dayOffset);
    const candidateDateStr = base.toISOString().slice(0, 10);
    const candidateInstant = new Date(`${candidateDateStr}T${gametime}:00Z`);
    if (fmt.format(candidateInstant) === gameday) return candidateInstant; // en-CA formats as YYYY-MM-DD
  }
  return null; // neither candidate round-tripped -- genuinely unknown, don't guess
}

// THE DATE AND THE TIME MUST COME OFF THE SAME CLOCK.
//
// REAL BUG (user report 2026-08-06, "the recommended bets are not in
// chronological order exactly"). This column used to print the date from
// `gameday` and the time from the real instant rendered in the VIEWER'S LOCAL
// zone. Those are two different clocks, so the two halves of one label could
// disagree by a day. A LoL match with gameday 2026-08-07 and instant
// 2026-08-07T00:00:00Z rendered as "Aug 7, 8:00 PM" for a viewer at UTC-4 --
// the date said Aug 7 (its UTC day) while 8:00 PM was Aug 6 locally.
//
// The rows were SORTED correctly the whole time, on the true instant. Only the
// labels lied, which made a correctly-ordered list read as scrambled: three
// evening rows shown under one date, then a 6:00 AM row shown under the same
// date, which is genuinely later. Nothing was wrong with the ordering, so no
// amount of sort-key fixing would ever have addressed the complaint.
//
// Deriving both halves from the same Date object is what makes the column
// monotonic. It also removes tennis's special case: tennis re-derived both
// already (its gameday is a discovery-time placeholder that disagrees with the
// instant by a full day), which was the correct behaviour for every sport all
// along, not a tennis quirk.
function localLabel(t: Date): string {
  return `${t.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, `
    + t.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function formatGameDate(
  gameday: string | null, gametime: string | null,
  sport: SportKey, homeTeam: string | null,
  estimatedStartTime?: string | null
) {
  if (!gameday) return <span className="text-[var(--color-text-muted)] italic">Season-long</span>;
  const d = new Date(gameday + "T00:00:00");
  const dateStr = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });

  // MMA, Tennis, Soccer, and all 3 esports titles (Valorant/CS2/LoL) use a
  // real, complete instant directly (Kalshi's own per-fight/per-match
  // occurrence_datetime, or Polymarket's equivalent gameStartTime -- see
  // backend TennisMatch/SoccerMatch/ValorantMatch/Cs2Match/LolMatch's own
  // estimated_start_time) rather than the gameday+gametime reconstruction
  // every other sport uses below -- none of these ever populate a separate
  // `gametime` string the way NFL/NBA/MLB do, so falling through to the
  // generic path below always hit the "no gametime -> date only"
  // early-return, silently showing NO time at all (caught live via user
  // report: "no time on recommended bets for soccer" -- Soccer was
  // originally left out of this branch entirely; the 3 esports titles hit
  // the SAME gap when their own Recommended page was built, caught live via
  // a second user report asking to reformat that page "including time").
  // For MMA/Soccer/esports, gameday (ufcstats' US-local event date /
  // Soccer's match_date / each esports title's own match_date) IS
  // trustworthy, so only the TIME portion uses the real instant --
  // re-deriving the date from the instant's own UTC day would show
  // "tomorrow" for evening fixtures that cross UTC midnight. Tennis is the
  // odd one out: its gameday is only a rough discovery-time placeholder (no
  // external schedule source exists, see market_catalog_tennis.py),
  // confirmed live to disagree with the real instant by a full calendar day
  // on a real match, so Tennis alone re-derives BOTH the date and time
  // labels from the real instant.
  //
  // DERIVED, NOT LISTED (2026-08-06) -- for the same reason sortKeyFor now is.
  // Racing was missing from this list too, so f1/nascar/irl rows fell through to
  // the "no gametime -> date only" early-return below and showed NO time at all,
  // sitting next to rows that did. Testing for the instant itself covers every
  // sport that has one, including the next one added.
  if (estimatedStartTime) {
    return <span className="whitespace-nowrap font-mono">{localLabel(new Date(estimatedStartTime))} (est.)</span>;
  }

  // "00:00" is nflverse's placeholder for "kickoff not yet announced" -- but
  // ONLY for NFL, whose gametime is stadium-LOCAL wall clock, where midnight is
  // not a real kickoff. Treating it as a placeholder everywhere suppressed the
  // most common tip-off time in the league:
  //
  //     nba   00:00 on 3,586 of 18,004 games (20%) -- the SINGLE most common,
  //           sitting among 01:00 (3,027), 00:30 (2,292), 23:00 (1,632)
  //     wnba  00:00 on 47 of 304 (15%) -- second most common
  //     nfl   00:00 on 12 of 7,597 (0.2%), against 13:00 on 3,798
  //
  // NBA/WNBA gametime is stored UTC (see the note below), so 00:00 UTC is
  // 8pm ET -- the standard US evening slot, not an unknown. User-reported:
  // "TOR vs Atlanta WNBA today" showing a date and no time.
  if (!gametime || (sport === "nfl" && gametime === "00:00")) {
    return <span className="whitespace-nowrap font-mono">{dateStr}</span>;
  }
  // NFL's gametime is stadium LOCAL kickoff (nflverse's own convention) --
  // parsed with no "Z" so the Date constructor treats it as wall-clock, which
  // toLocaleTimeString then just echoes back as typed. NBA's gametime is
  // stored UTC but self-consistent with gameday (both derive from the same
  // real UTC instant), so appending "Z" alone is correct. MLB's gameday/
  // gametime pairing is genuinely ambiguous (see mlbKickoffInstant above) --
  // resolved properly instead of naively appending "Z".
  let t: Date;
  if (sport === "nfl") {
    t = new Date(`${gameday}T${gametime}:00`);
  } else if (sport === "mlb") {
    const resolved = homeTeam ? mlbKickoffInstant(gameday, gametime, homeTeam) : null;
    if (resolved === null) return <span className="whitespace-nowrap font-mono">{dateStr}</span>; // unknown -- date only, don't guess a time
    t = resolved;
  } else {
    t = new Date(`${gameday}T${gametime}:00Z`);
  }
  return <span className="whitespace-nowrap font-mono">{localLabel(t)}</span>;
}


// REAL BUG fixed here (2026-07-17): the sort key used to be a naive
// `${gameday}T${gametime}` string concatenation -- correct for NFL
// (gametime is stadium-local) and NBA (gametime is UTC but self-consistent
// with gameday, both derived from the same real instant), but WRONG for
// MLB, whose gameday/gametime ambiguity this app has already fixed for
// display (mlbKickoffInstant above) and CLV (clv.py::_mlb_kickoff_utc) --
// the sort key was never updated to match, so an evening MLB game whose
// raw UTC gametime reads as an early-morning clock value (post-midnight)
// sorted as if it were early morning, ahead of that same day's afternoon
// games, instead of after them. Caught live via user report ("dates aren't
// in order"), not proactively -- same root cause, third fix site.
function sortKeyFor(row: RecommendedBetRow): string {
  if (!row.gameday) return "9999-12-31T23:59";
  if (row.sport === "mlb" && row.gametime) {
    const homeTeam = row.label.includes(" @ ") ? row.label.split(" @ ")[1] : null;
    const resolved = homeTeam ? mlbKickoffInstant(row.gameday, row.gametime, homeTeam) : null;
    if (resolved !== null) return resolved.toISOString();
  }
  // MMA/Tennis/Soccer/esports: a real, complete instant (see formatGameDate's
  // own comment on why it's not decomposed into gameday+gametime) -- without
  // this, every fight/match on the same placeholder gameday would fall back
  // to the same "23:59" placeholder and sort in an arbitrary/unspecified
  // order relative to each other instead of soonest-first. Soccer and the 3
  // esports titles (Valorant/CS2/LoL) were missing from this same check even
  // though formatGameDate's own display branch already covers them (Soccer
  // since 2026-07-19, esports since this Recommended page's own build) --
  // same display-only-not-sort-key gap already fixed once for MMA/Tennis,
  // caught again live here while fixing esports' missing time display.
  //
  // DERIVED, NOT LISTED (2026-08-06). This used to test a hardcoded sport list
  // -- mma/tennis/soccer/valorant/cs2/lol -- and RACING was never added to it,
  // so every f1/nascar/irl row fell through to the "23:59" placeholder and sorted
  // to the END of its own day despite carrying a real instant. Live at the time
  // of the fix: NASCAR 2026-08-09T22:30Z sorted after a 23:00Z fixture on the
  // same date. That is the FOURTH time this identical gap has been fixed by
  // appending one more sport to the list (MMA/Tennis, then Soccer, then the
  // three esports titles, now racing), each caught by a user noticing the order
  // was wrong. The list was always just a proxy for "this row has a real
  // instant", so ask that directly and a new sport is covered on arrival.
  if (row.estimatedStartTime) return row.estimatedStartTime;
  // Everything below reconstructs from gameday+gametime. KNOWN RESIDUAL, not
  // introduced here: NFL's gametime is stadium-LOCAL while every instant above
  // is UTC, so cross-sport ordering between NFL and other sports can be off by
  // the stadium's offset. Harmless today (NFL markets are pre-season and carry
  // "00:00" placeholders) but real once the season starts.
  return `${row.gameday}T${row.gametime ?? "23:59"}`;
}

// REAL UX PROBLEM fixed here (2026-07-18, user report: "hard to read all
// the data, have to scroll all the way right"): this table used to be 14
// separate columns (Date/Market/Type/Pick/Status/Source/Market %/Line
// Move/Est. %/Edge/Pool/Stake/Volume/Actions), measured live at 2008px
// wide against a 1280px viewport -- 728px of mandatory horizontal scroll
// just to see the stake/edge/actions on every row. Consolidated down to 7
// columns by grouping fields that are conceptually ONE piece of
// information (what's the bet / what's the price disagreement / what do
// I stake) into single stacked cells instead of spreading each field
// across its own column -- no information removed, just laid out
// vertically within a cell instead of horizontally across the table.
// Sorting: each merged column keeps ONE real accessor (the single most
// useful field to sort that group by -- edge for the price column, dollar
// stake for the stake column) since a table column can only sort by one
// value; the other fields in that same cell are still fully visible, just
// not independently sortable anymore (matches how "Status"/"Pool" were
// never independently useful sort keys anyway).
const columns = [
  // Sortable on a real chronological key so "soonest first" actually is.
  columnHelper.accessor((row) => sortKeyFor(row), {
    id: "date",
    header: "Date",
    cell: ({ row }) => {
      // label is always "AWAY @ HOME" for game-tied rows (see markets.py's
      // f"{away_team} @ {home_team}" construction) -- the only home-team
      // identifier this row carries, needed for MLB's timezone-aware fix.
      const homeTeam = row.original.label.includes(" @ ") ? row.original.label.split(" @ ")[1] : null;
      return formatGameDate(row.original.gameday, row.original.gametime, row.original.sport, homeTeam, row.original.estimatedStartTime);
    },
  }),
  // Merges the old Market + Type + Pick + Status columns -- a status icon
  // (Ready/Wait) prefixes the market label, the market type + plain-
  // English pick sit on a dimmer second line underneath.
  columnHelper.accessor("label", {
    header: "Bet",
    cell: ({ row }) => {
      const r = row.original;
      return (
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            {r.waitReason ? (
              <Hourglass size={12} className="text-[var(--color-warning)] shrink-0" />
            ) : (
              <CircleCheck size={12} className="text-[var(--color-good)] shrink-0" />
            )}
            <span className="font-medium whitespace-nowrap" title={r.waitReason ?? "Ready"}>{r.label}</span>
          </div>
          <div className="text-xs text-[var(--color-text-dim)] whitespace-nowrap mt-0.5">
            {marketTypeLabel(r.marketType, r.sport)} · {describePick(r)}
          </div>
          {/* REAL BUG this fixes (user-reported 2026-07-20: "LUA Gaming vs
              UB Alma Mater has a waiting symbol but I don't know why"): the
              Hourglass icon's only explanation was a native `title`
              attribute -- a hover-only tooltip with no visual cue that
              hovering reveals anything, easy to never discover at all
              (worse on touch devices, which have no hover). waitReason is a
              real, already-computed sentence (roster-change notes for
              esports, probable-pitcher/news-review gates elsewhere) that
              was simply never shown anywhere visible. Made a real, wrapped
              line here instead -- applies to every sport that sets
              waitReason since this table is shared, not an esports-only fix.*/}
          {r.waitReason && (
            <div className="flex items-start gap-1 text-xs text-[var(--color-warning)] mt-1 max-w-xs whitespace-normal">
              <Hourglass size={11} className="shrink-0 mt-0.5" />
              <span>{r.waitReason}</span>
            </div>
          )}
        </div>
      );
    },
  }),
  columnHelper.accessor("source", {
    header: "Source",
    cell: (info) => <SourceBadge source={info.getValue()} />,
  }),
  // Merges the old Market %/Line Move/Est. %/Edge columns -- market price
  // (with the 6h move inline, when it's moved enough to matter) sits above
  // the model's own estimate, edge badge underneath. Sorts by edge (the
  // single most useful "biggest disagreement first" ordering).
  // SHOWN FROM THE SIDE YOU ARE ACTUALLY BUYING.
  //
  // estProb/edge are stored in the YES frame on EVERY row, deliberately, so the
  // numbers mean one thing everywhere (see RecommendedBetRow.position). That is
  // right for the data and wrong for the display: a NO bet on a 92%-priced
  // "Over 2.5" rendered as "92% → 70%, -22pp", which reads as a bet the model
  // hates. It is the opposite -- buying NO at 8% against a model that says 30%,
  // a +22pp edge. User asked "why is a negative edge bet on my board", which is
  // exactly the confusion this caused.
  //
  // So NO rows are flipped to their own frame here, matching the "NO —" prefix
  // the label already carries. Everything else about the row is untouched.
  columnHelper.accessor((r) => (r.position === "no" ? -(r.edge ?? 0) : (r.edge ?? 0)), {
    id: "edge",
    header: () => <span title="Market price (6h move, if any) → this app's own model estimate, then the edge between them. NO bets are shown from the NO side.">Market → Model</span>,
    cell: ({ row }) => {
      const r = row.original;
      const isNo = r.position === "no";
      // The complement of every displayed number, and the price MOVE flips sign
      // too -- a YES price rising is a NO price falling.
      const shownImplied = isNo && r.impliedProb !== null ? 1 - r.impliedProb : r.impliedProb;
      const shownEst = isNo && r.estProb !== null ? 1 - r.estProb : r.estProb;
      const shownEdge = isNo && r.edge !== null ? -r.edge : r.edge;
      const rawMove = r.lineMovePp !== null ? r.lineMovePp * 100 : null;
      const movePp = rawMove !== null && isNo ? -rawMove : rawMove;
      return (
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-1 text-xs tabular-nums font-mono whitespace-nowrap">
            {isNo && <span className="text-[var(--color-text-muted)]" title="priced from the NO side">NO</span>}
            <span className="text-[var(--color-text-dim)]">{formatPct(shownImplied)}</span>
            {movePp !== null && Math.abs(movePp) >= 0.5 && (
              <span className={movePp > 0 ? "text-[var(--color-good)]" : "text-[var(--color-critical)]"}>
                {movePp > 0 ? "▲" : "▼"}{Math.abs(movePp).toFixed(1)}
              </span>
            )}
            <span className="text-[var(--color-text-muted)]">→</span>
            <span className="text-[var(--color-text-dim)]">{formatPct(shownEst)}</span>
          </div>
          <EdgeBadge edge={shownEdge} />
        </div>
      );
    },
  }),
  // Merges the old Pool + Stake columns -- pool tag sits under the stake
  // amount instead of getting its own column.
  columnHelper.accessor("suggestedStakeDollars", {
    header: () => <span title="Quarter Kelly, capped at 5% of its pool -- see Settings">Stake</span>,
    cell: ({ row }) => {
      const r = row.original;
      return (
        <div className="flex flex-col gap-1">
          <span className="tabular-nums font-mono text-[var(--color-good)] font-medium whitespace-nowrap">
            {/* 2dp below one unit: 1dp renders a 0.25u stake as "0.3u", a number
                nobody set. Above a unit 1dp is plenty. */}
            {r.suggestedStakeUnits !== null
              ? `${r.suggestedStakeUnits < 1 ? r.suggestedStakeUnits.toFixed(2) : r.suggestedStakeUnits.toFixed(1)}u`
              : "—"}
            <span className="text-[var(--color-text-muted)] ml-1 font-normal">
              (${r.suggestedStakeDollars.toLocaleString()})
            </span>
          </span>
          <div className="flex items-center gap-1.5">
            <span
              className={
                "inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-medium border whitespace-nowrap " +
                (r.stakePool === "futures"
                  ? "border-[#e59539]/40 text-[#e59539] bg-[#e59539]/10"
                  : "border-[#4fc3a1]/40 text-[#4fc3a1] bg-[#4fc3a1]/10")
              }
            >
              {r.stakePool === "futures" ? "Futures" : perGamePoolLabel(r.sport)}
            </span>
            <span className="text-[10px] text-[var(--color-text-muted)] whitespace-nowrap">{(r.kellyFraction * 100).toFixed(1)}% of pool</span>
          </div>
        </div>
      );
    },
  }),
  columnHelper.accessor("volume", {
    header: "Volume",
    cell: (info) => (
      <span className="tabular-nums font-mono text-[var(--color-text-dim)]">
        {info.getValue() ? Math.round(info.getValue()!).toLocaleString() : "—"}
      </span>
    ),
  }),
];

const sportColumn = columnHelper.accessor("sport", {
  header: "Sport",
  // Sport alone is too coarse to identify a row on the cross-sport views: TENNIS
  // could be a Grand Slam or an ITF futures match, VALORANT could be VCT or a
  // regional Challengers game. The league sits under it when the row has one.
  cell: ({ getValue, row }) => (
    <div className="leading-tight">
      <span className="block text-[10px] font-medium uppercase tracking-wide text-[var(--color-text-muted)]">{getValue()}</span>
      {row.original.league && (
        <span className="block text-[10px] text-[var(--color-text-dim)] truncate max-w-[130px]" title={row.original.league}>
          {row.original.league}
        </span>
      )}
    </div>
  ),
});

export function RecommendedBetsTable({
  rows,
  onMarkPlaced,
  onShowReasoning,
  placedMarketIds,
  showSport = false,
}: {
  rows: RecommendedBetRow[];
  onMarkPlaced: (row: RecommendedBetRow) => Promise<void>;
  onShowReasoning: (row: RecommendedBetRow) => void;
  placedMarketIds?: Set<string>; // cross-platform placed KEYS (see usePlacedKeys); Combined passes its own
  showSport?: boolean;
}) {
  const [sorting, setSorting] = useState<SortingState>([{ id: "date", desc: false }]);
  const [markingKeys, setMarkingKeys] = useState<Set<string>>(new Set());
  const [markedKeys, setMarkedKeys] = useState<Set<string>>(new Set());
  // Persistent "already placed" set: use the parent's if given (Combined passes
  // one), otherwise fetch it ourselves so every per-sport Recommended page also
  // remembers placed bets across navigation.
  const fetchedPlaced = usePlacedKeys(placedMarketIds === undefined);
  const effectivePlaced = placedMarketIds ?? fetchedPlaced;
  const navigate = useNavigate();
  // Hide "not ready yet" rows: far-future games + season-sport futures whose
  // season isn't active/near (same rule as the Discord alerts). Fails open
  // (shows everything) until readiness loads, so nothing flickers away wrongly.
  const readiness = useQuery({ queryKey: ["readiness"], queryFn: fetchReadiness }).data;
  const data = useMemo(() => rows.filter((r) => !isRowNotReady(r, readiness)), [rows, readiness]);

  async function handleMark(row: RecommendedBetRow) {
    setMarkingKeys((prev) => new Set(prev).add(row.key));
    try {
      await onMarkPlaced(row);
      setMarkedKeys((prev) => new Set(prev).add(row.key));
    } finally {
      setMarkingKeys((prev) => {
        const next = new Set(prev);
        next.delete(row.key);
        return next;
      });
    }
  }

  const actionColumn = columnHelper.display({
    id: "actions",
    header: "Actions",
    cell: ({ row }) => {
      const marking = markingKeys.has(row.original.key);
      // "marked" = tapped this session OR already recorded as placed (survives
      // remounts, so a bet you placed earlier still reads "Placed" on return).
      const rowGid = rowGameId(row.original);
      const marked =
        markedKeys.has(row.original.key) ||
        effectivePlaced.has(crossPlatformKey(row.original)) ||
        (rowGid !== null && effectivePlaced.has(`game:${rowGid}`));
      return (
        <div className="flex items-center gap-2">
          <button
            title="Why this bet"
            onClick={(e) => {
              e.stopPropagation();
              onShowReasoning(row.original);
            }}
            className="rounded-md border border-[var(--color-border)] p-1.5 hover:bg-[var(--color-surface-hover)] transition-colors"
          >
            <Info size={14} />
          </button>
          <button
            title={marked ? "Placed" : "Mark as placed"}
            disabled={marking || marked}
            onClick={(e) => {
              e.stopPropagation();
              handleMark(row.original);
            }}
            className={
              "rounded-md border px-2 py-1.5 text-xs font-medium inline-flex items-center gap-1 transition-colors " +
              (marked
                ? "border-[var(--color-good)]/40 text-[var(--color-good)] bg-[var(--color-good)]/10"
                : "border-[var(--color-border)] hover:bg-[var(--color-surface-hover)]")
            }
          >
            {marking ? <Loader2 size={14} className="animate-spin" /> : <CircleCheck size={14} />}
            {marked ? "Placed" : "Mark placed"}
          </button>
        </div>
      );
    },
  });

  const table = useReactTable({
    data,
    columns: [...(showSport ? [sportColumn] : []), ...columns, actionColumn],
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-x-auto">
      <table className="w-full text-sm border-collapse min-w-[820px]">
        <thead>
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id} className="border-b border-[var(--color-border)]">
              {hg.headers.map((header) => (
                <th
                  key={header.id}
                  onClick={header.column.getToggleSortingHandler()}
                  className="text-left px-4 py-3 text-xs uppercase tracking-wide text-[var(--color-text-dim)] font-medium cursor-pointer select-none hover:text-[var(--color-text)] transition-colors whitespace-nowrap"
                >
                  <span className="inline-flex items-center gap-1">
                    {flexRender(header.column.columnDef.header, header.getContext())}
                    {header.column.getIsSorted() && <ArrowUpDown size={12} />}
                  </span>
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => {
            const gameId = row.original.nflGameId;
            return (
              <tr
                key={row.id}
                onClick={gameId ? () => navigate(`/markets/${encodeURIComponent(gameId)}:${row.original.source}`) : undefined}
                className={
                  "border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-surface-hover)] transition-colors" +
                  (gameId ? " cursor-pointer" : "")
                }
              >
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-4 py-3 whitespace-nowrap">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            );
          })}
          {table.getRowModel().rows.length === 0 && (
            <tr>
              <td colSpan={columns.length + 1} className="px-4 py-10 text-center text-[var(--color-text-dim)]">
                No bets currently clear the staking threshold (edge below 20pp, or nothing priced yet).
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
