import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { fetchFuturesHistory } from "../../api/markets";

// Two series, one axis (both are probabilities, so they share a scale -- never a
// second y-axis). Hues validated against this app's dark surface #1c2027:
// lightness band, chroma floor, CVD separation (worst adjacent dE 20.5 protan,
// well clear of the 8 target), normal-vision floor and contrast all pass. Status
// colors are deliberately NOT reused here -- they mean good/warning/critical.
const MARKET_COLOR = "#b8873a";
const MODEL_COLOR = "#4a95ce";

type Point = { ts: string; prob: number };

function merge(market: Point[], model: Point[]) {
  const by = new Map<string, { t: number; label: string; market?: number; model?: number }>();
  const put = (rows: Point[], key: "market" | "model") => {
    for (const r of rows) {
      const d = new Date(r.ts);
      const bucket = r.ts.slice(0, 13); // hourly, matching how both are sampled
      const cur = by.get(bucket) ?? {
        t: d.getTime(),
        label: d.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
      };
      cur[key] = r.prob * 100;
      by.set(bucket, cur);
    }
  };
  put(market, "market");
  put(model, "model");
  return [...by.values()].sort((a, b) => a.t - b.t);
}

/** How a futures leg's price and the model's own number moved over time.
 *
 * A futures position settles months out, so between now and then the only thing
 * that happens is that opinion moves. Showing both lines answers the question
 * that actually matters for holding one: did the MODEL change its mind, or did
 * only the market? */
export function FuturesTrendModal({
  marketId, title, onClose,
}: { marketId: number; title: string; onClose: () => void }) {
  const { data, isLoading } = useQuery({
    queryKey: ["futures-history", marketId],
    queryFn: () => fetchFuturesHistory(marketId),
  });
  const rows = data ? merge(data.market, data.model) : [];
  const hasModel = rows.some((r) => r.model !== undefined);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="w-full max-w-2xl rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 mb-1">
          <div>
            <h3 className="text-sm font-medium text-[var(--color-text)]">{title}</h3>
            <p className="text-xs text-[var(--color-text-dim)] mt-0.5">
              Market price vs the model's own estimate, hourly.
            </p>
          </div>
          <button onClick={onClose} className="text-[var(--color-text-dim)] hover:text-[var(--color-text)]">
            <X size={16} />
          </button>
        </div>

        {isLoading ? (
          <div className="h-64 flex items-center justify-center text-sm text-[var(--color-text-dim)]">Loading…</div>
        ) : rows.length < 2 ? (
          <div className="h-64 flex items-center justify-center text-center text-sm text-[var(--color-text-dim)] px-6">
            Not enough history yet — this needs a few hours of readings before a trend means anything.
          </div>
        ) : (
          <>
            <div className="h-64 mt-3">
              <ResponsiveContainer width="100%" height="100%">
                {/* Top margin keeps a line near 100% off the plot edge. Futures legs
                    sit at the extremes far more often than mid-range, so a value
                    flush against the top is the common case, not the exception. */}
                <LineChart data={rows} margin={{ top: 12, right: 8, bottom: 0, left: -12 }}>
                  <CartesianGrid stroke="var(--color-border)" vertical={false} />
                  <XAxis
                    dataKey="label" tick={{ fontSize: 11, fill: "var(--color-text-muted)" }}
                    axisLine={{ stroke: "var(--color-border)" }} tickLine={false} minTickGap={28}
                  />
                  <YAxis
                    domain={[0, 100]} unit="%" width={44}
                    tick={{ fontSize: 11, fill: "var(--color-text-muted)" }}
                    axisLine={false} tickLine={false}
                  />
                  {/* formatter: recharts types the value as ValueType|undefined,
                      not number -- a gap in a connectNulls series really can hand
                      it an undefined, so that's handled rather than asserted away.
                      (Comment sits here, not among the attributes: a `//` line
                      comment between JSX attributes type-checks but does NOT
                      parse in Vite's transformer, and takes the whole app down.) */}
                  <Tooltip
                    contentStyle={{
                      background: "var(--color-surface-hover)", border: "1px solid var(--color-border)",
                      borderRadius: 6, fontSize: 12, color: "var(--color-text)",
                    }}
                    formatter={(v, name) => [typeof v === "number" ? `${v.toFixed(1)}%` : "—", name]}
                  />
                  <Legend wrapperStyle={{ fontSize: 12, color: "var(--color-text-dim)" }} />
                  <Line
                    type="monotone" dataKey="market" name="Market" stroke={MARKET_COLOR}
                    strokeWidth={2} dot={false} connectNulls
                  />
                  {hasModel && (
                    <Line
                      type="monotone" dataKey="model" name="Model" stroke={MODEL_COLOR}
                      strokeWidth={2} dot={false} connectNulls
                    />
                  )}
                </LineChart>
              </ResponsiveContainer>
            </div>
            {!hasModel && (
              <p className="text-xs text-[var(--color-text-dim)] mt-2">
                Only the market line so far — the model's own probability started being
                recorded recently, so its history builds from here.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
