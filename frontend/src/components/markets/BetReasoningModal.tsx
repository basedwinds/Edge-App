import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { X, TriangleAlert, ChevronDown, ChevronUp } from "lucide-react";
import { fetchMarketReasoning } from "../../api/markets";
import { EdgeBadge } from "./EdgeBadge";

function formatPct(v: number | null) {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

export function BetReasoningModal({
  marketId,
  modelProb,
  marketProb,
  sport = "nfl",
  onClose,
}: {
  marketId: number;
  modelProb: number | null;
  marketProb: number | null;
  sport?: "nfl" | "nba" | "wnba" | "mlb" | "mma" | "tennis" | "soccer" | "valorant" | "cs2" | "lol";
  onClose: () => void;
}) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["reasoning", sport, marketId, modelProb, marketProb],
    queryFn: () => fetchMarketReasoning(marketId, modelProb, marketProb, sport),
  });
  const [showDetails, setShowDetails] = useState(false);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 px-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg max-h-[85vh] overflow-y-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-4">
          <h3 className="text-lg font-semibold pr-4">{data?.label ?? "Why this bet"}</h3>
          <button onClick={onClose} className="text-[var(--color-text-dim)] hover:text-[var(--color-text)] transition-colors shrink-0">
            <X size={20} />
          </button>
        </div>

        {isLoading && <div className="text-sm text-[var(--color-text-dim)]">Loading…</div>}
        {isError && <div className="text-sm text-[var(--color-critical)]">Could not load reasoning for this market.</div>}

        {data && (
          <>
            <div className="flex items-center gap-4 mb-5 pb-4 border-b border-[var(--color-border)]">
              <div>
                <div className="text-xs text-[var(--color-text-dim)] uppercase tracking-wide mb-0.5">Market</div>
                <div className="text-lg font-semibold tabular-nums">{formatPct(data.market_prob)}</div>
              </div>
              <div>
                <div className="text-xs text-[var(--color-text-dim)] uppercase tracking-wide mb-0.5">Model</div>
                <div className="text-lg font-semibold tabular-nums text-[var(--color-text-dim)]">{formatPct(data.model_prob)}</div>
              </div>
              <div>
                <div className="text-xs text-[var(--color-text-dim)] uppercase tracking-wide mb-0.5">Edge</div>
                <EdgeBadge edge={data.edge} />
              </div>
            </div>

            <div className="mb-5">
              <p className="text-sm leading-relaxed">{data.insight}</p>
            </div>

            <button
              onClick={() => setShowDetails((v) => !v)}
              className="w-full flex items-center justify-between text-xs font-medium text-[var(--color-text-dim)] hover:text-[var(--color-text)] py-2 border-t border-[var(--color-border)] transition-colors"
            >
              {showDetails ? "Hide the numbers behind this" : "Show the numbers behind this"}
              {showDetails ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </button>

            {showDetails && (
              <div className="pt-3 space-y-5">
                <div>
                  <div className="text-sm font-medium mb-1.5">Methodology</div>
                  <p className="text-sm text-[var(--color-text-dim)]">{data.methodology}</p>
                </div>

                {data.factors.length > 0 && (
                  <div>
                    <div className="text-sm font-medium mb-2">Underlying facts</div>
                    <ul className="space-y-1.5">
                      {data.factors.map((f, i) => (
                        <li key={i} className="flex items-baseline justify-between gap-3 text-sm">
                          <span className="text-[var(--color-text-dim)]">{f.label}</span>
                          <span className="font-medium tabular-nums text-right">{f.detail}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {data.caveats.length > 0 && (
                  <div className="rounded-lg border border-[var(--color-warning)]/30 bg-[var(--color-warning)]/10 px-3 py-2.5 flex gap-2.5">
                    <TriangleAlert size={16} className="text-[var(--color-warning)] shrink-0 mt-0.5" />
                    <ul className="space-y-1 text-xs text-[var(--color-text-dim)]">
                      {data.caveats.map((c, i) => (
                        <li key={i}>{c}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
