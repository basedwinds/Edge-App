import { useQuery } from "@tanstack/react-query";
import { PageShell } from "../components/layout/PageShell";
import { TableSkeleton } from "../components/ui/Skeleton";
import { fetchClvBuckets, fetchPaperSummary, type ClvBucketRow } from "../api/markets";

export function ClvBuckets() {
  const query = useQuery({ queryKey: ["clv-buckets"], queryFn: fetchClvBuckets });
  const paperQuery = useQuery({ queryKey: ["paper-summary"], queryFn: fetchPaperSummary, refetchInterval: 60_000 });
  const rows: ClvBucketRow[] = query.data ?? [];
  const paper = paperQuery.data;

  return (
    <PageShell title="CLV Buckets">
      {query.isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}

      {paper && paper.total > 0 && (
        <div className="rounded-lg border border-[var(--color-accent)]/30 bg-[var(--color-accent)]/10 px-4 py-3 mb-6 text-sm">
          <div className="font-medium text-[var(--color-text)]">
            📈 Forward-CLV paper tracking is live — {paper.total.toLocaleString()} bets logged
          </div>
          <div className="text-xs text-[var(--color-text-dim)] mt-1">
            {paper.pending.toLocaleString()} awaiting their game close · {paper.with_clv.toLocaleString()} closed with real CLV so far.
            The app auto-logs every edge-qualified market as a paper bet (no real money) and measures whether we beat the
            closing line. Buckets below fill in as games settle. Coverage:{" "}
            {Object.entries(paper.by_sport).map(([s, n]) => `${s.toUpperCase()} ${n}`).join(" · ")}
          </div>
        </div>
      )}

      {query.isLoading ? (
        <TableSkeleton cols={5} />
      ) : rows.length === 0 ? (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-6 text-sm text-[var(--color-text-dim)]">
          No closed bets with real closing-line value yet. As placed bets settle against their closing
          prices, each (sport, market type) bucket accumulates here — and once a bucket reaches 20 closed
          bets, negative-CLV buckets get automatically suppressed from the recommended lists.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-[var(--color-border)]">
          <table className="w-full text-sm">
            <thead className="bg-[var(--color-surface)] text-[var(--color-text-dim)] text-xs">
              <tr>
                <th className="text-left px-3 py-2">Sport</th>
                <th className="text-left px-3 py-2">Market type</th>
                <th className="text-right px-3 py-2">Closed bets</th>
                <th className="text-right px-3 py-2">Avg CLV</th>
                <th className="text-left px-3 py-2">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--color-border)]">
              {rows.map((r) => (
                <tr key={`${r.sport}-${r.market_type}`} className="hover:bg-[var(--color-surface)]">
                  <td className="px-3 py-2 uppercase text-[var(--color-text-muted)]">{r.sport}</td>
                  <td className="px-3 py-2">{r.market_type}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{r.n}</td>
                  <td className={`px-3 py-2 text-right tabular-nums font-medium ${r.avg_clv_pp >= 0 ? "text-[var(--color-good)]" : "text-[var(--color-critical)]"}`}>
                    {(r.avg_clv_pp * 100).toFixed(1)}pp
                  </td>
                  <td className={`px-3 py-2 ${r.enabled ? "text-[var(--color-text-dim)]" : "text-[var(--color-critical)]"}`}>{r.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        Closing Line Value measures whether you got a better price than where the market ultimately closed —
        the earliest reliable signal of real edge, before win/loss has a big enough sample. Buckets stay
        enabled while gathering data (fewer than 20 closed bets); once well-sampled, a negative-average-CLV
        bucket is suppressed from recommendations automatically. This is how the app is meant to turn "no
        average edge" into "bet only the corners that actually beat the close."
      </p>
    </PageShell>
  );
}
