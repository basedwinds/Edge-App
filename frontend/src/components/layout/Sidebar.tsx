import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { LayoutDashboard, Settings, History, Trophy, Target, ClipboardList, Gauge, Layers, Bell, Flag, Wallet, Activity } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { fetchNewCatalogEntries } from "../../api/markets";

type Sport = "nfl" | "nba" | "wnba" | "mlb" | "mma" | "tennis" | "soccer" | "valorant" | "cs2" | "lol";

// Sport scope pills, grouped into labeled categories instead of one flat wrap
// of 10 -- `to` is each sport's landing route (NFL is the root dashboard; WNBA
// is moneyline-only so it lands on its Recommended page).
// key is a Sport for the scoped sports, or a racing-series slug ("f1"/"nascar"/
// "irl") for the Motorsport leagues, which route to /racing/<series> instead of
// having the full per-sport page set.
const SPORT_GROUPS: { label: string; sports: { key: string; label: string; to: string }[] }[] = [
  {
    label: "Team sports",
    sports: [
      { key: "nfl", label: "NFL", to: "/" },
      { key: "nba", label: "NBA", to: "/nba" },
      { key: "wnba", label: "WNBA", to: "/wnba/recommended" },
      { key: "mlb", label: "MLB", to: "/mlb" },
      { key: "soccer", label: "Soccer", to: "/soccer" },
    ],
  },
  {
    label: "Combat",
    sports: [{ key: "mma", label: "MMA", to: "/mma" }],
  },
  {
    label: "Racket",
    sports: [{ key: "tennis", label: "Tennis", to: "/tennis" }],
  },
  {
    label: "Esports",
    sports: [
      { key: "cs2", label: "CS2", to: "/cs2" },
      { key: "valorant", label: "Valorant", to: "/valorant" },
      { key: "lol", label: "LoL", to: "/lol" },
    ],
  },
  {
    label: "Motorsport",
    sports: [
      { key: "f1", label: "F1", to: "/racing/f1" },
      { key: "nascar", label: "NASCAR", to: "/racing/nascar" },
      { key: "irl", label: "IndyCar", to: "/racing/irl" },
    ],
  },
];

function pillClass(active: boolean): string {
  return clsx(
    "text-[10px] font-medium px-2 py-0.5 rounded-full transition-colors",
    active
      ? "bg-[var(--color-accent)] text-[#1c1408]"
      : "border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
  );
}

// Nav items are sport-scoped: clicking "Futures" (etc.) should take you to
// the CURRENT sport's futures page, not always NFL's -- matches the
// Sidebar's own original intent ("sport scope applies across every page
// below"). Backtests has no per-sport equivalent yet (NFL-only historical
// backtest scripts, not exposed as a page distinction) and Settings is
// already sport-agnostic internally (has nfl_*/nba_*/mlb_*/mma_*/tennis_*
// fields), so neither switches with sport scope. MLB has the full page set
// (Dashboard/Futures/Recommended/Placed/Calibration), same as NFL/NBA. MMA
// (2026-07-18) has Dashboard/Recommended/Placed/Calibration -- Futures
// deliberately excluded (KXUFCTITLE family is thin/illiquid compared to
// NFL's season futures, and a single UFC card already generates ~70+
// per-fight markets across 6 market types at once, so nearly all the real
// opportunity is per-fight; user asked for MMA futures last/low-priority).
// Tennis (2026-07-19) gained real futures too -- tournament-winner markets
// (KXATP/KXWTA), a bracket Monte Carlo off a real scraped draw
// (bracket_sim_tennis.py) rather than a season simulation, since Tennis has
// no season/schedule to simulate the way NFL/NBA/MLB do. MMA still has none
// (KXUFCTITLE family confirmed live 2026-07-19 to have literally zero open
// events on either platform -- not just thin, genuinely empty). Soccer
// (2026-07-19) gained League Winner + Relegation futures -- a double
// round-robin Monte Carlo (season_sim_soccer.py), the real fixture
// structure EPL/La Liga/Serie A/Bundesliga/Ligue 1 all use, so no real
// schedule/calendar source was needed the way it would be for NFL/NBA/MLB.
// Top-4/MLS Cup are NOT built (Top-4 had zero real open Kalshi events; MLS
// Cup is a single-elimination playoff, a different real structure this
// round-robin model doesn't cover) -- same "ship what has real inventory,
// flag the rest" precedent as MMA's own gap.
function navItems(sport: string) {
  const shared = [
    { to: "/all", label: "All Bets", icon: Layers, end: false },
    { to: "/tracker", label: "Bet Tracker", icon: Wallet, end: false },
    { to: "/new-markets", label: "New Markets", icon: Bell, end: false },
    // Divergences (cross-platform arb) shelved 2026-07-23 at user's request --
    // it's a day-trader feature (needs funded capital on BOTH platforms + fast
    // fills) that clashes with a once-a-day workflow. Route + scanner code kept
    // intact in App.tsx / the backend; just unlinked here. To bring it back,
    // re-add this line and the `Scale` icon to the lucide-react import above.
    // { to: "/divergences", label: "Divergences", icon: Scale, end: false },
    { to: "/clv-buckets", label: "CLV Tracker", icon: Gauge, end: false },
    { to: "/backtests", label: "Backtests", icon: History, end: false },
    { to: "/health", label: "Health Check", icon: Activity, end: false },
    { to: "/settings", label: "Settings", icon: Settings, end: false },
  ];
  // Motorsport leagues (F1/NASCAR/IndyCar) are one tracking-only markets page
  // each (the chip selects the series), so they get a single "Markets" link
  // rather than the full per-sport page set -- same reduced-nav idea as WNBA.
  if (sport === "f1" || sport === "nascar" || sport === "irl") {
    return { sportItems: [{ to: `/racing/${sport}`, label: "Markets", icon: Flag, end: false }], sharedItems: shared };
  }
  // WNBA is a moneyline-only, single-page integration (2026-07-22) -- only a
  // Recommended page exists (no Dashboard/Futures/Placed/Calibration yet), so
  // it gets just that one link rather than the full 5-page set.
  if (sport === "wnba") {
    return { sportItems: [{ to: "/wnba/recommended", label: "Recommended", icon: Target, end: false }], sharedItems: shared };
  }
  const prefix = sport === "nba" ? "/nba" : sport === "mlb" ? "/mlb" : sport === "mma" ? "/mma" : sport === "tennis" ? "/tennis" : sport === "soccer" ? "/soccer" : sport === "valorant" ? "/valorant" : sport === "cs2" ? "/cs2" : sport === "lol" ? "/lol" : "";
  const items = [{ to: prefix || "/", label: "Dashboard", icon: LayoutDashboard, end: true }];
  // Valorant/CS2/LoL (2026-07-19) each get their own markets Dashboard.
  // Recommended became a per-title route as of 2026-07-20, once each title
  // got its own independent bankroll pool (see settings.py::
  // VALORANT_ALLOCATION_PCT_KEY) -- previously all 3 shared ONE combined
  // page because they shared one pool; that's no longer true. Futures/
  // Placed/Calibration followed the same day, once each title's own real
  // tournament_winner futures inventory (model-less, same "real inventory,
  // no model" honesty as the Futures page text itself), manual-settlement
  // Placed Bets, and CLV-enabled Calibration were all wired up -- esports
  // now gets the exact same 5-page set as NFL/NBA/MLB/Tennis/Soccer (only
  // MMA is deliberately missing Futures, see below).
  if (sport !== "mma") {
    items.push({ to: `${prefix}/futures`, label: "Futures", icon: Trophy, end: false });
  }
  items.push(
    { to: `${prefix}/recommended`, label: "Recommended", icon: Target, end: false },
    { to: `${prefix}/placed`, label: "Placed Bets", icon: ClipboardList, end: false },
    { to: `${prefix}/calibration`, label: "Calibration", icon: Gauge, end: false },
  );
  return { sportItems: items, sharedItems: shared };
}

export function Sidebar() {
  // Same "flag anything unrecognized" data the Settings page shows in full
  // -- surfaced here too as a badge so a new market type doesn't sit
  // unnoticed until someone happens to open Settings (2026-07-16 user ask:
  // "a tab or something that lets me know when new markets have been
  // added"). Polls every 5 min, same cadence as the Settings page's own
  // fetch.
  const catalogQuery = useQuery({
    queryKey: ["catalog", "new"],
    queryFn: fetchNewCatalogEntries,
    refetchInterval: 5 * 60 * 1000,
  });
  const newCount = catalogQuery.data?.length ?? 0;

  // Sport-scope highlight needs its own logic, not NavLink's per-path
  // isActive -- "NFL" should stay highlighted across ALL of Dashboard/
  // Futures/Recommended/etc. (every page except /nba and /mlb), not just
  // "/" itself.
  const pathname = useLocation().pathname;
  const isNbaPath = pathname.startsWith("/nba");
  const isWnbaPath = pathname.startsWith("/wnba");
  const isMlbPath = pathname.startsWith("/mlb");
  const isMmaPath = pathname.startsWith("/mma");
  const isTennisPath = pathname.startsWith("/tennis");
  const isSoccerPath = pathname.startsWith("/soccer");
  const isValorantPath = pathname.startsWith("/valorant");
  const isCs2Path = pathname.startsWith("/cs2");
  const isLolPath = pathname.startsWith("/lol");
  const isRacingPath = pathname.startsWith("/racing");
  const racingSeries = isRacingPath ? (pathname.split("/")[2] || "f1") : null;  // /racing/f1 -> "f1"
  const isSharedPath = pathname === "/backtests" || pathname === "/settings" || pathname === "/divergences" || pathname === "/clv-buckets" || pathname === "/all" || pathname === "/tracker" || pathname === "/new-markets" || pathname === "/health";

  // Backtests/Settings are sport-agnostic (shared across every sport), so
  // pathname alone can't tell which sport scope to show there. Without this,
  // the pill (and Dashboard/Futures/etc links) would silently snap back to
  // NFL the moment you left another sport for one of these pages -- reported
  // 2026-07-17 ("when i go to backtests it takes me back to NFL tab").
  // Remembering the last NON-shared sport instead keeps the scope stable.
  const currentSport: string = isRacingPath ? (racingSeries as string) : isMlbPath ? "mlb" : isWnbaPath ? "wnba" : isNbaPath ? "nba" : isMmaPath ? "mma" : isTennisPath ? "tennis" : isSoccerPath ? "soccer" : isValorantPath ? "valorant" : isCs2Path ? "cs2" : isLolPath ? "lol" : "nfl";
  const [lastSport, setLastSport] = useState<string>(currentSport);
  useEffect(() => {
    if (!isSharedPath) {
      setLastSport(currentSport);
    }
  }, [currentSport, isSharedPath]);

  const activeSport: string = isSharedPath ? lastSport : currentSport;
  const { sportItems, sharedItems } = navItems(activeSport);

  return (
    <aside className="w-56 shrink-0 border-r border-[var(--color-border)] bg-[var(--color-bg)] flex flex-col">
      <div className="px-4 pt-5 pb-4">
        <div className="font-serif text-[15px] text-[var(--color-text)] tracking-tight">Edge Finder</div>
        <div className="text-[10.5px] text-[var(--color-text-muted)] mt-0.5">multi-sport edge tracking</div>
      </div>

      {/* Sport scope -- applies across every page below. Grouped into
          categories so 10 sports read as an organized list, not one flat wrap. */}
      <div className="px-4 pb-4 space-y-2.5">
        {SPORT_GROUPS.map((group) => (
          <div key={group.label}>
            <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">{group.label}</div>
            <div className="flex flex-wrap gap-1.5">
              {group.sports.map((s) => (
                <NavLink key={s.key} to={s.to} className={pillClass(activeSport === s.key)}>
                  {s.label}
                </NavLink>
              ))}
            </div>
          </div>
        ))}
      </div>

      <div className="mx-4 border-t border-[var(--color-border)]" />

      <nav className="flex-1 px-2 py-3 overflow-y-auto">
        <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-muted)] px-3 mb-1">
          {SPORT_GROUPS.flatMap((g) => g.sports).find((s) => s.key === activeSport)?.label ?? "This sport"}
        </div>
        <div className="space-y-0.5">
          {sportItems.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                clsx(
                  "flex items-center gap-2.5 border-l-2 px-3 py-1.5 text-[13px] transition-colors",
                  isActive
                    ? "border-l-[var(--color-accent)] text-[var(--color-text)]"
                    : "border-l-transparent text-[var(--color-text-dim)] hover:text-[var(--color-text)]"
                )
              }
            >
              <Icon size={15} />
              {label}
            </NavLink>
          ))}
        </div>

        <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-muted)] px-3 mt-4 mb-1">Cross-sport</div>
        <div className="space-y-0.5">
          {sharedItems.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                clsx(
                  "flex items-center gap-2.5 border-l-2 px-3 py-1.5 text-[13px] transition-colors",
                  isActive
                    ? "border-l-[var(--color-accent)] text-[var(--color-text)]"
                    : "border-l-transparent text-[var(--color-text-dim)] hover:text-[var(--color-text)]"
                )
              }
            >
              <Icon size={15} />
              {label}
              {to === "/new-markets" && newCount > 0 && (
                <span className="ml-auto inline-flex items-center justify-center min-w-[18px] h-[18px] rounded-full bg-[var(--color-warning)] text-[10px] font-semibold text-black px-1">
                  {newCount}
                </span>
              )}
            </NavLink>
          ))}
        </div>
      </nav>

      <div className="px-4 py-3 text-[10.5px] text-[var(--color-text-muted)] border-t border-[var(--color-border)]">
        v0.1 — dev build
      </div>
    </aside>
  );
}
