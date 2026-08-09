/** Single source of truth for sport metadata on the frontend -- the mirror of
 *  backend/app/sports.py.
 *
 *  WHY THIS EXISTS. Adding a sport meant editing several independent lists, and
 *  each one failed SILENTLY when missed. CFB was left out of three separate
 *  places in one session, none of which was a type error:
 *
 *    - rowGameId          -> the per-game bet cap quietly stopped applying, so a
 *                            single game could surface several correlated bets.
 *    - Sidebar currentSport -> /cfb fell through to the "nfl" fallback, so the
 *                            sub-nav showed NFL's links on College Football pages.
 *    - (backend) alert endpoints -> the sport never alerted and never accrued CLV.
 *
 *  Derive from SPORTS below rather than retyping a list. */

/** Every sport key that can appear on a bet row, as a const tuple so the union
 *  below derives from it. THIS IS THE ONLY PLACE THE LIST IS WRITTEN.
 *
 *  There were 14 hand-copied versions of this union across 10 files, in four
 *  different shapes -- some missing "cfb", some missing the racing series, one
 *  missing "wnba". None of them agreed, and the drift only surfaced as a broken
 *  `npm run build` (vite dev does not typecheck, so the app ran fine meanwhile).
 *
 *  The racing series (f1/irl/nascar) are bet-row sports but are NOT in SPORTS
 *  below: they share one /racing route, so they have no per-sport nav entry and
 *  no game-id field. That is why this list is broader than SPORTS. */
export const SPORT_KEYS = [
  "nfl", "nba", "wnba", "cfb", "mlb", "soccer", "mma", "tennis",
  "valorant", "cs2", "lol", "cod", "f1", "irl", "nascar",
] as const;

export type SportKey = (typeof SPORT_KEYS)[number];

/** The three racing series. They share the /racing/:series route, so the series
 *  arrives as a raw URL string and has to be narrowed before it can be used as a
 *  SportKey -- an unrecognised /racing/xyz would otherwise query the backend with
 *  whatever the URL said. */
export const RACING_SERIES = ["f1", "irl", "nascar"] as const;
export type RacingSeries = (typeof RACING_SERIES)[number];

export function isRacingSeries(v: string | undefined): v is RacingSeries {
  return v !== undefined && (RACING_SERIES as readonly string[]).includes(v);
}

export interface SportMeta {
  key: SportKey;
  label: string;
  /** Route prefix. NFL is the root dashboard, so its prefix is "" . */
  routePrefix: string;
  /** The RecommendedBetRow field carrying this sport's real-world event id, if
   *  any. Used by rowGameId for the per-game cap. */
  gameIdField?: keyof GameIdFields;
  /** Whether ids from this sport need namespacing before comparison. Esports and
   *  CFB use numeric/ESPN ids that can collide across sports; NFL/NBA/MLB ids are
   *  already globally unique strings. */
  prefixGameId?: boolean;
  /** True where the sport has its own /futures PAGE. CFB is false on purpose --
   *  its futures are market types inside /cfb/markets, not a separate endpoint. */
  hasFuturesPage: boolean;
}

/** The id fields a recommended row can carry. Kept as a type so gameIdField is
 *  checked at compile time -- a typo here is a build error, not a silent miss. */
export interface GameIdFields {
  nflGameId: string | null;
  nbaGameId?: string | null;
  wnbaGameId?: string | null;
  cfbGameId?: string | null;
  mlbGameId?: string | null;
  mmaFightId?: string | null;
  tennisMatchId?: number | null;
  soccerMatchId?: number | null;
  valorantMatchId?: number | null;
  cs2MatchId?: number | null;
  lolMatchId?: number | null;
  codMatchId?: number | null;
}

export const SPORTS: SportMeta[] = [
  { key: "nfl", label: "NFL", routePrefix: "", gameIdField: "nflGameId", hasFuturesPage: true },
  { key: "nba", label: "NBA", routePrefix: "/nba", gameIdField: "nbaGameId", hasFuturesPage: true },
  { key: "wnba", label: "WNBA", routePrefix: "/wnba", gameIdField: "wnbaGameId", hasFuturesPage: false },
  { key: "cfb", label: "College Football", routePrefix: "/cfb", gameIdField: "cfbGameId", prefixGameId: true, hasFuturesPage: false },
  { key: "mlb", label: "MLB", routePrefix: "/mlb", gameIdField: "mlbGameId", hasFuturesPage: true },
  { key: "soccer", label: "Soccer", routePrefix: "/soccer", gameIdField: "soccerMatchId", hasFuturesPage: true },
  { key: "mma", label: "MMA", routePrefix: "/mma", gameIdField: "mmaFightId", hasFuturesPage: false },
  { key: "tennis", label: "Tennis", routePrefix: "/tennis", gameIdField: "tennisMatchId", hasFuturesPage: true },
  { key: "valorant", label: "Valorant", routePrefix: "/valorant", gameIdField: "valorantMatchId", prefixGameId: true, hasFuturesPage: true },
  { key: "cs2", label: "CS2", routePrefix: "/cs2", gameIdField: "cs2MatchId", prefixGameId: true, hasFuturesPage: true },
  { key: "lol", label: "LoL", routePrefix: "/lol", gameIdField: "lolMatchId", prefixGameId: true, hasFuturesPage: true },
  // hasFuturesPage false: Kalshi lists no CoD futures at all (KXCODGAME is
  // match-winner only), so a Futures tab here would always be empty.
  { key: "cod", label: "Call of Duty", routePrefix: "/cod", gameIdField: "codMatchId", prefixGameId: true, hasFuturesPage: false },
];

/** Which sport a pathname belongs to, or null. Replaces the hand-written
 *  startsWith chain in Sidebar that defaulted to "nfl" -- that default is what
 *  made a missing sport look like NFL rather than an error.
 *
 *  Longest prefix wins so a future "/nba-x" style route can't shadow "/nba". */
export function sportFromPath(pathname: string): string | null {
  const hit = SPORTS
    .filter((s) => s.routePrefix && pathname.startsWith(s.routePrefix))
    .sort((a, b) => b.routePrefix.length - a.routePrefix.length)[0];
  return hit ? hit.key : null;
}

/** The namespaced real-world event id for a row, or null when it isn't tied to
 *  one (season-long futures). Derived from SPORTS so a new sport is covered by
 *  registering it, not by remembering to edit this. */
export function gameIdForRow(
  row: Partial<GameIdFields> & { fixtureKey?: number | null },
): string | null {
  for (const s of SPORTS) {
    if (!s.gameIdField) continue;
    const raw = row[s.gameIdField];
    if (raw === null || raw === undefined || raw === "") continue;
    // fixtureKey collapses the TWO rows an esports match can have (a Kalshi
    // one and a Polymarket one spelling a team differently) onto one id. They
    // used to look like two independent games here, which quietly disabled the
    // per-match stake cap for exactly the matches most likely to be doubled.
    const id = row.fixtureKey ?? raw;
    return s.prefixGameId ? `${s.key}:${id}` : String(id);
  }
  return null;
}
