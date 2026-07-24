import { ArrowUpRight, ArrowDownRight, Minus } from "lucide-react";
import clsx from "clsx";

const NEUTRAL_THRESHOLD = 0.02;

/** Diverging encoding, not good/bad: positive = model sees the home side as
 * more likely than the market prices it (blue), negative = the reverse (red),
 * near-zero = neutral gray. Deliberately NOT green/red "good/bad" -- the
 * model isn't validated to beat the market (see MarketDetail disclaimer), so
 * this shows disagreement direction/magnitude, not a trade signal. */
export function EdgeBadge({ edge }: { edge: number | null }) {
  if (edge === null) {
    return <span className="text-[var(--color-text-muted)] text-sm">—</span>;
  }

  const pct = edge * 100;
  const isNeutral = Math.abs(edge) < NEUTRAL_THRESHOLD;
  const isPositive = edge > 0;

  const Icon = isNeutral ? Minus : isPositive ? ArrowUpRight : ArrowDownRight;

  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-sm font-mono font-medium tabular-nums",
        isNeutral && "bg-white/5 text-[var(--color-text-dim)]",
        !isNeutral && isPositive && "bg-[var(--color-accent)]/15 text-[var(--color-accent)]",
        !isNeutral && !isPositive && "bg-[var(--color-critical)]/15 text-[var(--color-critical)]"
      )}
    >
      <Icon size={13} />
      {pct > 0 ? "+" : ""}
      {pct.toFixed(1)}pp
    </span>
  );
}
