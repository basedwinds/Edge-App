import clsx from "clsx";

const LABELS: Record<string, string> = {
  kalshi: "Kalshi",
  polymarket: "Polymarket",
};

export function SourceBadge({ source }: { source: string }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium border",
        source === "kalshi" && "border-[#3987e5]/40 text-[#3987e5] bg-[#3987e5]/10",
        source === "polymarket" && "border-[#9085e9]/40 text-[#9085e9] bg-[#9085e9]/10"
      )}
    >
      {LABELS[source] ?? source}
    </span>
  );
}
