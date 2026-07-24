import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, XCircle, Info, CheckCircle2, RefreshCw } from "lucide-react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchHealthCheck, type HealthIssue } from "../api/health";

const SPORT_LABEL: Record<string, string> = {
  nfl: "NFL", nba: "NBA", wnba: "WNBA", mlb: "MLB", mma: "MMA", tennis: "Tennis",
  soccer: "Soccer", valorant: "Valorant", cs2: "CS2", lol: "LoL", f1: "F1", nascar: "NASCAR", irl: "IndyCar",
};
const CATEGORY_LABEL: Record<string, string> = {
  stale_poller: "Stale data feed",
  unlinked_markets: "Unlinked tickers",
  no_market_price: "No market price",
  no_schedule: "No games scheduled",
  race_date_mismatch: "Wrong race date",
};

const SEV = {
  error: { icon: XCircle, cls: "text-[var(--color-critical)]", bg: "border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10" },
  warning: { icon: AlertTriangle, cls: "text-[var(--color-warning)]", bg: "border-[var(--color-warning)]/30 bg-[var(--color-warning)]/10" },
  info: { icon: Info, cls: "text-[var(--color-text-dim)]", bg: "border-[var(--color-border)] bg-[var(--color-surface)]" },
} as const;

function IssueRow({ issue }: { issue: HealthIssue }) {
  const s = SEV[issue.severity];
  const Icon = s.icon;
  return (
    <div className={`flex items-start gap-3 rounded-lg border px-4 py-3 ${s.bg}`}>
      <Icon size={16} className={`${s.cls} shrink-0 mt-0.5`} />
      <div className="min-w-0">
        <div className="text-sm">
          <span className="font-medium">{CATEGORY_LABEL[issue.category] ?? issue.category}</span>
          <span className="text-[var(--color-text-dim)]"> · {SPORT_LABEL[issue.sport] ?? issue.sport}</span>
        </div>
        <div className="text-xs text-[var(--color-text-dim)] mt-0.5">{issue.detail}</div>
      </div>
    </div>
  );
}

export function Health() {
  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["health-check"],
    queryFn: fetchHealthCheck,
    refetchOnWindowFocus: true,
  });

  return (
    <PageShell title="Health Check">
      <div className="flex items-center justify-between mb-4">
        <p className="text-xs text-[var(--color-text-muted)] max-w-2xl">
          Automated data-integrity scan — catches stalled feeds, markets that can't be priced, unlinked
          tickers, empty schedules, and race dates that disagree with the real calendar. Check it when you
          sit down (or remotely) to confirm nothing's silently broken.
        </p>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 rounded-md border border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)] disabled:opacity-50 shrink-0"
        >
          <RefreshCw size={13} className={isFetching ? "animate-spin" : ""} /> Re-run
        </button>
      </div>

      {isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend — is it running?
        </div>
      )}

      {isLoading || !data ? (
        <>
          <StatTilesSkeleton count={3} />
          <TableSkeleton cols={1} />
        </>
      ) : (
        <>
          <div className="flex flex-wrap border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] mb-6 divide-x divide-[var(--color-border)]">
            <StatTile label="Errors" value={String(data.counts.error)} sublabel="likely bugs — look now" />
            <StatTile label="Warnings" value={String(data.counts.warning)} sublabel="worth a glance" />
            <StatTile label="Info" value={String(data.counts.info)} sublabel="expected / context" />
          </div>

          {data.issues.length === 0 ? (
            <div className="flex items-center gap-2 rounded-lg border border-[var(--color-good)]/30 bg-[var(--color-good)]/10 px-4 py-6 text-sm text-[var(--color-good)]">
              <CheckCircle2 size={18} /> All clear — no data-integrity issues detected.
            </div>
          ) : (
            <div className="space-y-2">
              {data.issues.map((issue, i) => (
                <IssueRow key={i} issue={issue} />
              ))}
            </div>
          )}

          <p className="text-[11px] text-[var(--color-text-muted)] mt-4">
            Last checked {new Date(data.checked_at).toLocaleString()}. "No games scheduled" and "No market price"
            on racing are usually expected (off-season / Kalshi not quoting yet), not bugs.
          </p>
        </>
      )}
    </PageShell>
  );
}
