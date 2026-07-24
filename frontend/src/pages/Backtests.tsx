import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { PageShell } from "../components/layout/PageShell";
import { fetchBacktests, runAllBacktests, runOneBacktest, type BacktestEntry } from "../api/backtests";

function formatRunAt(iso: string | null | undefined): string {
  if (!iso) return "never run";
  const then = new Date(iso + "Z").getTime(); // backend sends naive UTC ISO, no offset
  const seconds = Math.floor((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function BacktestCard({ entry, onRerun, rerunning }: { entry: BacktestEntry; onRerun: (key: string) => void; rerunning: boolean }) {
  const { result } = entry;
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] mb-4">
      <div className="flex items-start justify-between gap-4 px-4 py-3 border-b border-[var(--color-border)]">
        <div>
          <div className="text-sm font-medium text-[var(--color-text)]">{entry.label}</div>
          <div className="text-xs text-[var(--color-text-muted)] mt-0.5 max-w-2xl">{entry.summary}</div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-xs text-[var(--color-text-muted)]" title={result?.run_at ?? undefined}>
            {formatRunAt(result?.run_at)}
            {result ? ` · ${result.duration_sec}s` : ""}
          </span>
          <button
            onClick={() => onRerun(entry.key)}
            disabled={rerunning}
            className="inline-flex items-center gap-1.5 rounded-md border border-[var(--color-border)] px-2 py-1 text-xs text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-hover)] disabled:opacity-50 transition-colors"
          >
            <RefreshCw size={11} className={rerunning ? "animate-spin" : ""} />
            {rerunning ? "Running…" : "Rerun"}
          </button>
        </div>
      </div>
      <div className="px-4 py-3">
        {result ? (
          <pre className="text-[11px] leading-relaxed text-[var(--color-text-dim)] whitespace-pre-wrap font-mono overflow-x-auto">
            {result.output}
          </pre>
        ) : (
          <div className="text-xs text-[var(--color-text-muted)] italic">Not run yet.</div>
        )}
      </div>
    </div>
  );
}

export function Backtests() {
  const queryClient = useQueryClient();
  const [runningAll, setRunningAll] = useState(false);
  const [runningKey, setRunningKey] = useState<string | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["backtests"],
    queryFn: fetchBacktests,
  });

  async function handleRunAll() {
    setRunningAll(true);
    try {
      await runAllBacktests();
      await queryClient.invalidateQueries({ queryKey: ["backtests"] });
    } finally {
      setRunningAll(false);
    }
  }

  async function handleRerun(key: string) {
    setRunningKey(key);
    try {
      await runOneBacktest(key);
      await queryClient.invalidateQueries({ queryKey: ["backtests"] });
    } finally {
      setRunningKey(null);
    }
  }

  const nfl = (data ?? []).filter((e) => e.sport === "nfl");
  const nba = (data ?? []).filter((e) => e.sport === "nba");
  const mlb = (data ?? []).filter((e) => e.sport === "mlb");
  const mma = (data ?? []).filter((e) => e.sport === "mma");
  const tennis = (data ?? []).filter((e) => e.sport === "tennis");

  return (
    <PageShell title="Backtests">
      {isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}

      <div className="flex items-center justify-between mb-4">
        <p className="text-xs text-[var(--color-text-muted)] max-w-2xl">
          Walk-forward validation for every pricing model, run against real historical outcomes. NFL checks
          compare the model against the de-vigged market closing line where that history exists; NBA and MLB
          checks are calibration-only (no free historical odds source exists for either sport to backtest
          against — see each card). Results are cached, not re-run automatically — running all 10 takes
          roughly 45 seconds.
        </p>
        <button
          onClick={handleRunAll}
          disabled={runningAll || runningKey !== null}
          className="inline-flex items-center gap-1.5 rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-hover)] disabled:opacity-50 transition-colors shrink-0 ml-4"
        >
          <RefreshCw size={12} className={runningAll ? "animate-spin" : ""} />
          {runningAll ? "Running all…" : "Run all backtests"}
        </button>
      </div>

      {isLoading ? (
        <div className="text-sm text-[var(--color-text-muted)]">Loading…</div>
      ) : (
        <>
          <div className="text-xs font-medium text-[var(--color-text-muted)] uppercase tracking-wide mb-2">NFL</div>
          {nfl.map((entry) => (
            <BacktestCard key={entry.key} entry={entry} onRerun={handleRerun} rerunning={runningKey === entry.key} />
          ))}

          <div className="text-xs font-medium text-[var(--color-text-muted)] uppercase tracking-wide mb-2 mt-6">NBA</div>
          {nba.map((entry) => (
            <BacktestCard key={entry.key} entry={entry} onRerun={handleRerun} rerunning={runningKey === entry.key} />
          ))}

          <div className="text-xs font-medium text-[var(--color-text-muted)] uppercase tracking-wide mb-2 mt-6">MLB</div>
          {mlb.map((entry) => (
            <BacktestCard key={entry.key} entry={entry} onRerun={handleRerun} rerunning={runningKey === entry.key} />
          ))}

          <div className="text-xs font-medium text-[var(--color-text-muted)] uppercase tracking-wide mb-2 mt-6">MMA</div>
          {mma.map((entry) => (
            <BacktestCard key={entry.key} entry={entry} onRerun={handleRerun} rerunning={runningKey === entry.key} />
          ))}

          <div className="text-xs font-medium text-[var(--color-text-muted)] uppercase tracking-wide mb-2 mt-6">Tennis</div>
          {tennis.map((entry) => (
            <BacktestCard key={entry.key} entry={entry} onRerun={handleRerun} rerunning={runningKey === entry.key} />
          ))}
        </>
      )}
    </PageShell>
  );
}
