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
import { perGamePoolLabel, fetchOpenBets, fetchSettledBets, fetchReadiness, isRowNotReady, crossPlatformKey, rowGameId, type RecommendedBetRow } from "../../api/markets";

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
      const [open, settled] = await Promise.all([fetchOpenBets(), fetchSettledBets()]);
      const keys = new Set<string>();
      for (const b of [...open, ...settled]) {
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
import { MARKET_TYPE_LABELS } from "./FuturesTable";
import { SourceBadge } from "./SourceBadge";
import { EdgeBadge } from "./EdgeBadge";

const GAME_MARKET_TYPE_LABELS: Record<string, string> = {
  moneyline: "Moneyline",
  spread: "Spread",
  total: "Total",
  team_total: "Team Total",
  spread_1h: "1st Half Spread",
  spread_2h: "2nd Half Spread",
  total_1h: "1st Half Total",
  total_2h: "2nd Half Total",
  f5: "First 5 Innings",
  rfi: "Run in 1st Inning",
  distance: "Goes the Distance",
  method_of_victory: "Method of Victory",
  method_of_finish: "Method of Finish",
  rounds: "Round of Finish",
  round_of_victory: "Round of Victory",
  // Soccer (added 2026-07-19) -- own distinct market_type strings, not
  // reusable with NFL/NBA/MLB's bare "spread"/"total"/"moneyline" above.
  moneyline_3way: "Moneyline",
  game_spread: "Spread",
  game_total: "Total",
  btts: "BTTS",
  ftts: "1st To Score",
  correct_score: "Correct Score",
  first_half_winner: "1H Winner",
  first_half_spread: "1H Spread",
  first_half_total: "1H Total",
  first_half_team_total: "1H Team Total",
  first_half_btts: "1H BTTS",
  second_half_winner: "2H Winner",
  second_half_spread: "2H Spread",
  second_half_total: "2H Total",
  second_half_team_total: "2H Team Total",
  second_half_btts: "2H BTTS",
  // Esports (Valorant/CS2/LoL) -- fell through to the raw market_type
  // string until now (found live 2026-07-20 while adding real coverage for
  // all 3 titles' own KXGAME series winner tickers), same wording as each
  // sport's own Dashboard page (Cs2.tsx/Valorant.tsx/Lol.tsx's own
  // MARKET_TYPE_LABELS).
  map_winner: "Map Winner",
  series_winner: "Series Winner",
  series_total: "Total Maps",
  series_handicap: "Map Handicap",
};

function marketTypeLabel(marketType: string): string {
  return GAME_MARKET_TYPE_LABELS[marketType] ?? MARKET_TYPE_LABELS[marketType] ?? marketType;
}

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

function formatGameDate(
  gameday: string | null, gametime: string | null,
  sport: "nfl" | "nba" | "mlb" | "mma" | "tennis" | "soccer" | "valorant" | "cs2" | "lol", homeTeam: string | null,
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
  if (sport === "mma" || sport === "tennis" || sport === "soccer" || sport === "valorant" || sport === "cs2" || sport === "lol") {
    if (!estimatedStartTime) return <span className="whitespace-nowrap font-mono">{dateStr}</span>; // not yet known (far out) -- date only, don't guess
    const t = new Date(estimatedStartTime);
    const timeStr = t.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    const labelDateStr = sport === "tennis" ? t.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : dateStr;
    return <span className="whitespace-nowrap font-mono">{labelDateStr}, {timeStr} (est.)</span>;
  }

  // "00:00" is nflverse/ESPN's placeholder for "kickoff not yet announced"
  // (confirmed live: 18/108 tracked NFL games show it, alongside genuine real
  // times like "13:00"/"20:15") -- not an actual midnight kickoff.
  if (!gametime || gametime === "00:00") return <span className="whitespace-nowrap font-mono">{dateStr}</span>;
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
  const timeStr = t.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return <span className="whitespace-nowrap font-mono">{dateStr}, {timeStr}</span>;
}

// Spells out exactly what "Yes"/winning this bet means, in plain English --
// added 2026-07-17 after user feedback that the old "team ± line" rendering
// was genuinely ambiguous. The underlying `line` field means "this team's
// OWN margin must exceed `line`" for every spread-shaped market in this app
// (see game_lines.py/game_lines_nba.py/game_lines_mlb.py's prob_team_covers
// -- same convention on all three sports), which does NOT match standard
// bookmaker "-1.5 favorite / +1.5 underdog" notation: a favorite's line here
// is POSITIVE (must win by more than N), an underdog's is NEGATIVE (must
// not lose by N or more). Showing the raw signed number as "TEAM -2.5"
// reads backwards to anyone using normal sports-betting intuition, so this
// spells out the real threshold in words instead of leaning on a sign
// convention at all.
function describeSpreadPick(team: string, line: number): string {
  if (line > 0) return `${team} wins by ${Math.ceil(line)}+`;
  if (line < 0) return `${team} doesn't lose by ${Math.ceil(Math.abs(line))}+`;
  return `${team} wins outright`;
}

function describePick(row: RecommendedBetRow): string {
  const { team, line, side, marketType, sport } = row;
  if (marketType === "f5") return side === "tie" ? "Tie after 5 innings" : `${team ?? "—"} wins first 5 innings`;
  if (marketType === "rfi") return side === "no" ? "No run in the 1st inning" : "A run scores in the 1st inning";
  if (marketType === "distance") return "Fight goes the distance";
  if (marketType === "method_of_finish") {
    return { kotko: "KO/TKO", submission: "Submission", decision: "Decision" }[side ?? ""] ?? side ?? "—";
  }
  if (marketType === "rounds" && line !== null) {
    return side === "under" ? `Ends before round ${line}` : `Goes past round ${line}`;
  }
  if (marketType === "set_winner" && line !== null) return `${team ?? "—"} wins Set ${Math.round(line)}`;
  if (marketType === "exact_score" && side) return `${team ?? "—"} wins ${side}`;
  // REAL BUG fixed here (caught live via user report: "over 3.5 games" shown
  // for a Soccer total): "game_total" is used by BOTH Tennis (total GAMES
  // across a set/match) and Soccer (total GOALS in a match) -- two sports
  // sharing the identical market_type string for genuinely different real
  // quantities. Every other consumer of this string already disambiguates by
  // sport (e.g. buildSoccerRecommendedBets' own SOCCER_LADDER_TYPES), this
  // label just never did.
  if (marketType === "game_total" && line !== null) return `Over ${line} ${sport === "soccer" ? "goals" : "games"}`;
  // Second batch (added 2026-07-19) -- Soccer's own FTTS/correct_score/
  // half-family market types, none of which fit any of the generic
  // patterns below (FTTS/half-winner are 3-way-but-not-moneyline_3way;
  // correct_score needs the real scoreline, which lives in its own two
  // fields, not team/line/side; half-spread doesn't match the plain
  // "spread"-prefix check below since its market_type string is
  // "first_half_spread"/"second_half_spread", not "spread_...").
  if (marketType === "btts" || marketType === "first_half_btts" || marketType === "second_half_btts") {
    const halfNote = marketType === "first_half_btts" ? " (1st half)" : marketType === "second_half_btts" ? " (2nd half)" : "";
    return `Both teams to score${halfNote}`;
  }
  if (marketType === "ftts") return side === "none" ? "Neither team scores first" : (team ?? "—");
  if (marketType === "correct_score") {
    return row.correctScoreHome != null && row.correctScoreAway != null
      ? `${row.correctScoreHome} - ${row.correctScoreAway}`
      : "—";
  }
  if (marketType === "moneyline_3way" || marketType === "first_half_winner" || marketType === "second_half_winner") {
    const halfNote = marketType === "first_half_winner" ? " (1st half)" : marketType === "second_half_winner" ? " (2nd half)" : "";
    return (side === "draw" ? "Draw" : (team ?? "—")) + halfNote;
  }
  if ((marketType === "first_half_spread" || marketType === "second_half_spread") && line !== null && team) {
    const halfNote = marketType === "first_half_spread" ? " (1st half)" : " (2nd half)";
    return `${team} wins by ${Math.ceil(line)}+ goals${halfNote}`;
  }
  if (marketType === "set_total" && line !== null) {
    const setLabel = side ? side.replace("set_", "Set ") : "?";
    return `${setLabel}: Over ${line} games`;
  }
  if (marketType === "total_sets" && line !== null) return `Over ${line} sets`;
  // REAL BUG fixed here (caught live via user report: "B2U over 1" shown for
  // a LoL map-winner pick, genuinely unreadable): this table's generic
  // fallback below (built for NFL/NBA/MLB's plain team+line markets) doesn't
  // know about the 3 esports titles' own map_winner/series_handicap/
  // series_total market types, so it silently mislabeled all three as a
  // bare "team Over/Under line" -- the individual sport Dashboard pages
  // (Lol.tsx/Cs2.tsx/Valorant.tsx's own formatPick) already spell these out
  // correctly, this table's shared describePick just never got the same 3
  // cases. map_winner's `line` is a map NUMBER (not a threshold), so
  // "Over"/"Under" is nonsensical for it regardless of source sport.
  if (marketType === "map_winner" && line !== null) return `${team ?? "—"} wins Map ${Math.round(line)}`;
  // series_handicap only exists on Valorant today, but written sport-
  // agnostic like every other market_type check here. Same signed-margin
  // convention as describeSpreadPick above (see elo_valorant.py's
  // prob_handicap_cover_a/b) -- spelled out in words for the same reason
  // describeSpreadPick is, rather than echoing raw_line's sign.
  if (marketType === "series_handicap" && line !== null && team) {
    if (line > 0) return `${team} wins by ${Math.ceil(line)}+ maps`;
    if (line < 0) return `${team} doesn't lose by ${Math.ceil(Math.abs(line))}+ maps`;
    return `${team} wins outright`;
  }
  if (marketType === "series_total" && line !== null) return `${side === "under" ? "Under" : "Over"} ${line} maps`;
  if (line === null) return team ?? "—";
  if (marketType.startsWith("spread") || marketType === "game_spread") {
    if (sport === "soccer" && team) return `${team} wins by ${Math.ceil(line)}+ goals`;
    return team ? describeSpreadPick(team, line) : String(line);
  }
  // total/team_total/half-totals AND every remaining ladder market (win_total,
  // season-stat thresholds, division win-totals) all resolve on an Over/Under
  // (or "at least N") threshold -- `side` is "over" for every one of these
  // except real Polymarket/Kalshi totals, which can genuinely be "under".
  const sideLabel = side === "under" ? "Under" : "Over";
  return team ? `${team} ${sideLabel} ${line}` : `${sideLabel} ${line}`;
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
  if (
    (row.sport === "mma" || row.sport === "tennis" || row.sport === "soccer"
      || row.sport === "valorant" || row.sport === "cs2" || row.sport === "lol")
    && row.estimatedStartTime
  ) {
    return row.estimatedStartTime;
  }
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
            {marketTypeLabel(r.marketType)} · {describePick(r)}
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
  columnHelper.accessor("edge", {
    header: () => <span title="Market price (6h move, if any) → this app's own model estimate, then the edge between them">Market → Model</span>,
    cell: ({ row }) => {
      const r = row.original;
      const movePp = r.lineMovePp !== null ? r.lineMovePp * 100 : null;
      return (
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-1 text-xs tabular-nums font-mono whitespace-nowrap">
            <span className="text-[var(--color-text-dim)]">{formatPct(r.impliedProb)}</span>
            {movePp !== null && Math.abs(movePp) >= 0.5 && (
              <span className={movePp > 0 ? "text-[var(--color-good)]" : "text-[var(--color-critical)]"}>
                {movePp > 0 ? "▲" : "▼"}{Math.abs(movePp).toFixed(1)}
              </span>
            )}
            <span className="text-[var(--color-text-muted)]">→</span>
            <span className="text-[var(--color-text-dim)]">{formatPct(r.estProb)}</span>
          </div>
          <EdgeBadge edge={r.edge} />
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
            {r.suggestedStakeUnits !== null ? `${r.suggestedStakeUnits.toFixed(1)}u` : "—"}
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
                No bets currently clear the staking threshold (edge below 3pp, or nothing priced yet).
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
