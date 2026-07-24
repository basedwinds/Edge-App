export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={"animate-pulse rounded bg-white/5 " + className} />;
}

export function TableSkeleton({ rows = 7, cols = 6 }: { rows?: number; cols?: number }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-hidden">
      <div className="border-b border-[var(--color-border)] px-4 py-3 flex gap-6">
        {Array.from({ length: cols }).map((_, c) => (
          <Skeleton key={c} className="h-3 w-16" />
        ))}
      </div>
      <div className="p-4 space-y-4">
        {Array.from({ length: rows }).map((_, r) => (
          <div key={r} className="flex gap-6 items-center">
            {Array.from({ length: cols }).map((_, c) => (
              <Skeleton key={c} className={"h-4 " + (c === 0 ? "w-24" : "w-14")} />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

export function StatTilesSkeleton({ count = 4 }: { count?: number }) {
  return (
    <div className="flex flex-wrap gap-4 mb-6">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-5 py-4 flex-1 min-w-[160px]">
          <Skeleton className="h-3 w-20 mb-2.5" />
          <Skeleton className="h-7 w-16 mb-1.5" />
          <Skeleton className="h-3 w-28" />
        </div>
      ))}
    </div>
  );
}
