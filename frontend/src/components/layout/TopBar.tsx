import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { fetchHealth, triggerRefresh } from "../../api/markets";

function formatRelativeTime(iso: string | null): string {
  if (!iso) return "never";
  const then = new Date(iso + "Z").getTime(); // backend sends naive UTC ISO, no offset
  const seconds = Math.floor((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function TopBar({ title }: { title: string }) {
  const queryClient = useQueryClient();
  const [justTriggered, setJustTriggered] = useState(false);

  // Polls every 30s so "Updated Xm ago" stays roughly accurate without
  // needing a page reload -- cheap, this is a single tiny row lookup.
  const { data } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 30 * 1000,
  });

  async function handleRefresh() {
    setJustTriggered(true);
    await triggerRefresh();
    // The actual refresh runs in the background over ~30s -- refetch
    // everything once a reasonable amount of time has passed, and let the
    // 30s health poll above eventually reflect the real completion time.
    setTimeout(() => {
      queryClient.invalidateQueries();
      setJustTriggered(false);
    }, 5000);
  }

  return (
    <header className="h-14 shrink-0 border-b border-[var(--color-border)] flex items-center justify-between px-6">
      <h1 className="font-serif text-lg text-[var(--color-text)] tracking-tight">{title}</h1>
      <div className="flex items-center gap-3">
        <span className="text-xs text-[var(--color-text-muted)]" title={data?.last_refresh_at ?? undefined}>
          Updated {formatRelativeTime(data?.last_refresh_at ?? null)}
        </span>
        <button
          onClick={handleRefresh}
          disabled={justTriggered}
          title="Trigger an immediate data refresh"
          className="inline-flex items-center gap-1.5 rounded-md border border-[var(--color-border)] px-2.5 py-1.5 text-xs text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-hover)] disabled:opacity-50 transition-colors"
        >
          <RefreshCw size={12} className={justTriggered ? "animate-spin" : ""} />
          {justTriggered ? "Refreshing…" : "Refresh"}
        </button>
      </div>
    </header>
  );
}
