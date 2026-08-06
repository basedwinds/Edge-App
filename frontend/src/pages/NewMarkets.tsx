import { useQuery, useQueryClient } from "@tanstack/react-query";
import { PageShell } from "../components/layout/PageShell";
import {
  fetchNewCatalogEntries, fetchFlaggedCatalogEntries,
  dismissCatalogEntry, resolveFlaggedCatalogEntry,
} from "../api/markets";
import { SourceBadge } from "../components/markets/SourceBadge";

const SPORT_LABELS: Record<string, string> = {
  nfl: "NFL", nba: "NBA", wnba: "WNBA", mlb: "MLB", mma: "MMA",
  tennis: "Tennis", soccer: "Soccer", valorant: "Valorant", cs2: "CS2", lol: "LoL",
  // Catch-all bucket from the backend scan (catalog_scan.py's fetch_kalshi_other_series):
  // a live Kalshi Sports series belonging to no sport this app tracks or ingests.
  other: "Untracked sport",
};

// Auto-triage tag (backend catalog_classify.py). Colors: green = the one
// auto-priceable bucket (a clean head-to-head outcome an existing model could
// handle); everything else is a "needs review / no model for this" reason.
const CATEGORY_META: Record<string, { label: string; cls: string }> = {
  match_outcome: { label: "Match outcome", cls: "text-[var(--color-positive)] border-[var(--color-positive)]/40 bg-[var(--color-positive)]/10" },
  news: { label: "News / media", cls: "text-[var(--color-text-dim)] border-[var(--color-border)]" },
  stat_leader: { label: "Player prop", cls: "text-[var(--color-text-dim)] border-[var(--color-border)]" },
  futures: { label: "Futures / bracket", cls: "text-[var(--color-warning)] border-[var(--color-warning)]/40 bg-[var(--color-warning)]/10" },
  review: { label: "Review", cls: "text-[var(--color-text-dim)] border-[var(--color-border)]" },
};

function CategoryTag({ category, note, autoPriceable }: { category: string; note: string; autoPriceable: boolean }) {
  const meta = CATEGORY_META[category] ?? CATEGORY_META.review;
  return (
    <span
      title={note}
      className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium ${meta.cls}`}
    >
      {autoPriceable ? "✓ " : ""}{meta.label}
    </span>
  );
}

// Standalone page (promoted out of Settings, 2026-07-23) so newly-detected
// markets are front-and-center in the sidebar with their own count badge,
// instead of buried under Settings. Same data + actions as before: the daily
// catalog scan flags series/events that don't match a market type this app
// already prices, and the human triages them here. Nothing auto-builds -- see
// the "never guess a number" rule -- but see the "Auto-priced" note below for
// the subset the scan can now safely route into an existing model.
export function NewMarkets() {
  const queryClient = useQueryClient();

  const catalogQuery = useQuery({
    queryKey: ["catalog", "new"],
    queryFn: fetchNewCatalogEntries,
    refetchInterval: 5 * 60 * 1000,
  });
  const flaggedQuery = useQuery({
    queryKey: ["catalog", "flagged"],
    queryFn: fetchFlaggedCatalogEntries,
    refetchInterval: 5 * 60 * 1000,
  });

  async function handleDismiss(id: number, disposition: "not_relevant" | "flagged") {
    await dismissCatalogEntry(id, disposition);
    queryClient.invalidateQueries({ queryKey: ["catalog", "new"] });
    queryClient.invalidateQueries({ queryKey: ["catalog", "flagged"] });
  }

  async function handleResolveFlagged(id: number) {
    await resolveFlaggedCatalogEntry(id);
    queryClient.invalidateQueries({ queryKey: ["catalog", "flagged"] });
  }

  return (
    <PageShell title="New Markets">
      {(catalogQuery.isError || flaggedQuery.isError) && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}

      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-5 py-5 max-w-2xl">
        <div className="text-sm font-medium text-[var(--color-text)] mb-1">New markets detected</div>
        <div className="text-xs text-[var(--color-text-dim)] mb-4">
          Kalshi/Polymarket series or events across every tracked sport that showed up since the last daily
          catalog scan and don't match a market type this app already prices. New games/matches within an
          already-supported sport aren't listed here — those get priced automatically and just appear in
          Recommended. What's left needs a human to review its resolution rules first (the "never guess a
          number" rule), so nothing here auto-builds. Pick "Not relevant" to dismiss, or "Flag to build" to
          keep it in the backlog below.
        </div>

        {catalogQuery.isLoading ? (
          <div className="text-sm text-[var(--color-text-dim)]">Loading…</div>
        ) : catalogQuery.data && catalogQuery.data.length > 0 ? (
          <ul className="space-y-2">
            {catalogQuery.data.map((entry) => (
              <li
                key={entry.id}
                className="flex items-center justify-between gap-3 rounded-md border border-[var(--color-border)] px-3 py-2"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <SourceBadge source={entry.platform} />
                    <span className="text-xs font-medium text-[var(--color-text-muted)]">{SPORT_LABELS[entry.sport] ?? entry.sport}</span>
                    <CategoryTag category={entry.category} note={entry.category_note} autoPriceable={entry.auto_priceable} />
                    <span className="text-sm font-medium truncate">{entry.title}</span>
                  </div>
                  <div className="text-xs text-[var(--color-text-muted)] mt-0.5">
                    {entry.identifier} · first seen {new Date(entry.first_seen).toLocaleDateString()}
                  </div>
                  <div className="text-[11px] text-[var(--color-text-dim)] mt-1">{entry.category_note}</div>
                </div>
                <div className="flex shrink-0 gap-2">
                  <button
                    onClick={() => handleDismiss(entry.id, "flagged")}
                    className="rounded-md border border-[var(--color-accent)]/40 text-[var(--color-accent)] px-3 py-1.5 text-xs font-medium hover:bg-[var(--color-accent)]/10 transition-colors"
                  >
                    Flag to build
                  </button>
                  <button
                    onClick={() => handleDismiss(entry.id, "not_relevant")}
                    className="rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs font-medium text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-hover)] transition-colors"
                  >
                    Not relevant
                  </button>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-sm text-[var(--color-text-dim)]">
            Nothing new — every currently-tracked series/event has been reviewed.
          </div>
        )}
      </div>

      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-5 py-5 max-w-2xl mt-6">
        <div className="text-sm font-medium text-[var(--color-text)] mb-1">Flagged for future work</div>
        <div className="text-xs text-[var(--color-text-dim)] mb-4">
          Series/events marked "worth building" above. This is a persistent to-do list, not a queue this app
          works through on its own. Mark resolved once you've actually built ingestion/model support for it
          (or decided against it after all).
        </div>

        {flaggedQuery.isLoading ? (
          <div className="text-sm text-[var(--color-text-dim)]">Loading…</div>
        ) : flaggedQuery.data && flaggedQuery.data.length > 0 ? (
          <ul className="space-y-2">
            {flaggedQuery.data.map((entry) => (
              <li
                key={entry.id}
                className="flex items-center justify-between gap-3 rounded-md border border-[var(--color-border)] px-3 py-2"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <SourceBadge source={entry.platform} />
                    <span className="text-xs font-medium text-[var(--color-text-muted)]">{SPORT_LABELS[entry.sport] ?? entry.sport}</span>
                    <CategoryTag category={entry.category} note={entry.category_note} autoPriceable={entry.auto_priceable} />
                    <span className="text-sm font-medium truncate">{entry.title}</span>
                  </div>
                  <div className="text-xs text-[var(--color-text-muted)] mt-0.5">
                    {entry.identifier} · flagged {new Date(entry.first_seen).toLocaleDateString()}
                  </div>
                  {/* The whole point of the backlog: WHY it's waiting and what
                      would unblock it. Without this a flagged row is
                      indistinguishable from one nobody has looked at, which is
                      exactly the "am I just staring at a list that sits there?"
                      problem this list is supposed to solve. */}
                  {entry.note && (
                    <div className="text-xs text-[var(--color-text-dim)] mt-1.5 leading-relaxed border-l-2 border-[var(--color-border)] pl-2">
                      {entry.note}
                    </div>
                  )}
                </div>
                <button
                  onClick={() => handleResolveFlagged(entry.id)}
                  className="shrink-0 rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs font-medium text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-hover)] transition-colors"
                >
                  Mark resolved
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-sm text-[var(--color-text-dim)]">
            Nothing flagged for future work.
          </div>
        )}
      </div>
    </PageShell>
  );
}
