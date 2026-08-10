import { useQuery } from "@tanstack/react-query";
import { fetchWarmup } from "../../api/markets";

/** Shown while the backend is still building its in-memory model state.
 *
 * WHY: the rating services start empty after every restart and fill on the
 * first poll. Until then a sport's page either sits on an endless spinner (the
 * request is genuinely rebuilding 240k soccer matches) or renders a full table
 * of blank model columns. Neither told the user anything, and the blanks in
 * particular are indistinguishable from "no edges today".
 *
 * Deliberately a BANNER rather than a full-screen block: most pages are partly
 * usable while warming -- placed bets, settings and the tracker read the DB and
 * are unaffected -- so hiding everything behind a splash would be a downgrade
 * for the pages that already work.
 */
export function WarmingBanner() {
  const { data } = useQuery({
    queryKey: ["warmup"],
    queryFn: fetchWarmup,
    // Poll while cold so the banner clears itself, then stop: once ready, the
    // answer cannot go back to false without a restart, which remounts this.
    refetchInterval: (q) => (q.state.data?.ready ? false : 5000),
    // KEEP POLLING WHEN THE TAB IS NOT FOCUSED. react-query pauses interval
    // refetches in the background by default, and that is precisely wrong here:
    // waiting out a multi-minute boot is exactly when someone tabs away. Caught
    // in testing -- the backend reported ready and the banner stayed up until
    // the page was touched, which is the "stuck loading screen" this is meant
    // to prevent. Costs one 0.5s request every 5s, and only while cold.
    refetchIntervalInBackground: true,
    staleTime: 0,
    retry: false,
  });

  if (!data || data.ready) return null;

  const pct = data.total > 0 ? Math.round((data.warm / data.total) * 100) : 0;
  // Server-supplied: it excludes sports that are simply out of season, which a
  // client-side filter over `services` would wrongly list as still loading.
  const pending = data.pending ?? [];

  return (
    <div
      role="status"
      aria-live="polite"
      className="border-b border-[var(--color-border)] bg-[var(--color-surface)] px-6 py-3"
    >
      <div className="flex items-center gap-3">
        <span
          aria-hidden
          className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-[var(--color-border)] border-t-[var(--color-accent)]"
        />
        <div className="min-w-0 text-sm">
          <span className="font-medium">Building model state — {data.warm} of {data.total} sports ready.</span>{" "}
          <span className="text-[var(--color-text-dim)]">
            Ratings are rebuilt in memory after each restart, so model columns stay blank until a
            sport finishes. Numbers already shown are real; missing ones are not yet computed.
          </span>
        </div>
      </div>
      <div className="mt-2 h-1 w-full overflow-hidden rounded bg-[var(--color-border)]">
        <div
          className="h-full bg-[var(--color-accent)] transition-[width] duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
      {pending.length > 0 && (
        <div className="mt-1.5 text-xs text-[var(--color-text-muted)]">
          still warming: {pending.join(", ")}
        </div>
      )}
    </div>
  );
}
