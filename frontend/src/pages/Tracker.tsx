import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Info } from "lucide-react";
import { PageShell } from "../components/layout/PageShell";
import { StatTile } from "../components/markets/StatTile";
import { BetReasoningModal } from "../components/markets/BetReasoningModal";
import { StatTilesSkeleton, TableSkeleton } from "../components/ui/Skeleton";
import { fetchPortfolio, fetchOpenBets, fetchSettledBets, fetchSettings, type PortfolioPointPayload, type OpenBetPayload, type SettledBetPayload } from "../api/markets";
import { futuresResolution, gameResolution } from "../utils/resolution";
import { futuresMarketName, futuresThreshold } from "../utils/futuresLabel";

// Sports whose /reasoning endpoint exists (racing has no reasoning route yet).
type ReasoningSport = "nfl" | "nba" | "wnba" | "mlb" | "mma" | "tennis" | "soccer" | "valorant" | "cs2" | "lol";
const REASONING_SPORTS = new Set<string>(["nfl", "nba", "wnba", "mlb", "mma", "tennis", "soccer", "valorant", "cs2", "lol"]);
type ReasoningTarget = { marketId: number; sport: ReasoningSport; modelProb: number | null; marketProb: number | null };

// Small "why this number?" button — only rendered when the bet still has a live
// market_id AND its sport has a reasoning route. Opens the shared explanation
// modal (same one the market pages use), so the tracker reads back the model's
// case for every position, not just its price.
function ExplainButton({ target, onExplain }: { target: ReasoningTarget | null; onExplain: (t: ReasoningTarget) => void }) {
  if (!target) return null;
  return (
    <button
      onClick={() => onExplain(target)}
      className="p-1 rounded-md border border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)] hover:border-[var(--color-accent)]"
      title="Why this bet? (model explanation)"
    >
      <Info size={13} />
    </button>
  );
}

// Build a reasoning target from a placed-bet payload, or null when it can't be
// explained (missing market id, or a sport with no reasoning route).
function reasoningTarget(b: { market_id: number; sport: string; model_prob_at_placement: number | null; market_prob_at_placement: number | null }): ReasoningTarget | null {
  if (!b.market_id || !REASONING_SPORTS.has(b.sport)) return null;
  return { marketId: b.market_id, sport: b.sport as ReasoningSport, modelProb: b.model_prob_at_placement, marketProb: b.market_prob_at_placement };
}

// The bet-diary replacement: one cross-sport view of realized P/L, ROI,
// win/loss record and average CLV, all off the SAME placed-bet snapshots the
// per-sport Placed pages settle. Realized P/L and ROI are computed on the
// backend (/placed-bets/portfolio) from each bet's OWN placement price, so
// price capture (CLV) and money outcome (P/L) sit side by side here.
const SPORT_LABEL: Record<string, string> = {
  nfl: "NFL", nba: "NBA", wnba: "WNBA", mlb: "MLB", mma: "MMA",
  tennis: "Tennis", soccer: "Soccer", valorant: "Valorant", cs2: "CS2", lol: "LoL",
  f1: "F1", nascar: "NASCAR", irl: "IndyCar",
};
const SOURCE_LABEL: Record<string, string> = { kalshi: "Kalshi", polymarket: "Polymarket" };

function money(n: number): string {
  const sign = n > 0 ? "+" : n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function units(n: number): string {
  return `${n > 0 ? "+" : n < 0 ? "-" : ""}${Math.abs(n).toFixed(2)}u`;
}
function pnlClass(n: number): string {
  return n > 0 ? "text-[var(--color-good)]" : n < 0 ? "text-[var(--color-critical)]" : "text-[var(--color-text-dim)]";
}

type CurveMode = "dollars" | "units";

// Inline SVG cumulative-P/L curve -- no charting dependency (matches the
// app's no-CDN, self-contained convention). A flat zero baseline is drawn so
// above/below water is obvious at a glance. `mode` toggles $ vs units.
function EquityCurve({ points, mode }: { points: PortfolioPointPayload[]; mode: CurveMode }) {
  const W = 720, H = 200, PAD = 8;
  const path = useMemo(() => {
    if (points.length === 0) return null;
    const val = (p: PortfolioPointPayload) => (mode === "dollars" ? p.cumulative_profit_dollars : p.cumulative_profit_units);
    // Anchor at a 0 origin so even a single settlement day draws a real climb
    // from zero (a lone point would render as nothing) -- and it's the standard
    // equity-curve shape anyway (start flat at 0, then run up/down).
    const ys = [0, ...points.map(val)];
    const lo = Math.min(0, ...ys);
    const hi = Math.max(0, ...ys);
    const span = hi - lo || 1;
    const n = ys.length;
    const x = (i: number) => PAD + (i * (W - 2 * PAD)) / (n - 1);
    const y = (v: number) => PAD + (H - 2 * PAD) * (1 - (v - lo) / span);
    const line = ys.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const area = `${line} L${x(n - 1).toFixed(1)},${y(lo).toFixed(1)} L${x(0).toFixed(1)},${y(lo).toFixed(1)} Z`;
    const last = ys[ys.length - 1];
    return { line, area, zeroY: y(0), last, endX: x(n - 1), endY: y(last) };
  }, [points, mode]);

  if (!path) {
    return (
      <div className="h-[200px] flex items-center justify-center text-sm text-[var(--color-text-muted)] border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)]">
        No settled bets yet — the P/L curve fills in as bets settle.
      </div>
    );
  }
  const stroke = path.last >= 0 ? "var(--color-good)" : "var(--color-critical)";
  return (
    <div className="border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] p-3">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-[200px]" preserveAspectRatio="none">
        <defs>
          <linearGradient id="pnlFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity="0.18" />
            <stop offset="100%" stopColor={stroke} stopOpacity="0" />
          </linearGradient>
        </defs>
        <line x1={PAD} y1={path.zeroY} x2={W - PAD} y2={path.zeroY} stroke="var(--color-border)" strokeWidth="1" strokeDasharray="3 3" />
        <path d={path.area} fill="url(#pnlFill)" />
        <path d={path.line} fill="none" stroke={stroke} strokeWidth="1.75" strokeLinejoin="round" strokeLinecap="round" />
        <circle cx={path.endX} cy={path.endY} r="3" fill={stroke} />
      </svg>
    </div>
  );
}

// "Starts in 3h", "Starts in 12m", "Live / started", or a date for far-out bets.
function startLabel(iso: string | null): { text: string; soon: boolean } {
  if (!iso) return { text: "—", soon: false };
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return { text: "—", soon: false };
  const diff = ms - Date.now();
  if (diff <= 0) return { text: "started", soon: false };
  const mins = Math.round(diff / 60000);
  if (mins < 60) return { text: `in ${mins}m`, soon: true };
  const hrs = mins / 60;
  if (hrs < 24) return { text: `in ${Math.round(hrs)}h`, soon: hrs < 6 };
  const days = Math.round(hrs / 24);
  return { text: `in ${days}d`, soon: false };
}

// The actual local start time next to the countdown, e.g. "Jul 26, 2:00 PM".
function startAbsolute(iso: string | null): string {
  if (!iso) return "";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "";
  return new Date(ms).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function OpenPositions({ bets, onExplain }: { bets: OpenBetPayload[]; onExplain: (t: ReasoningTarget) => void }) {
  if (bets.length === 0) {
    return (
      <div className="text-sm text-[var(--color-text-muted)] border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] px-4 py-6 text-center">
        No open positions. Bets you mark placed show up here until they settle.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto border border-[var(--color-border)] rounded-lg">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--color-text-dim)] border-b border-[var(--color-border)]">
            <th className="px-3 py-2 font-medium">Starts</th>
            <th className="px-3 py-2 font-medium">Settles</th>
            <th className="px-3 py-2 font-medium">Sport</th>
            <th className="px-3 py-2 font-medium">Bet</th>
            <th className="px-3 py-2 font-medium">Source</th>
            <th className="px-3 py-2 font-medium text-right">Entry</th>
            <th className="px-3 py-2 font-medium text-right">Stake</th>
            <th className="px-3 py-2 font-medium"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--color-border)]">
          {bets.map((b) => {
            const s = startLabel(b.start_time);
            return (
              <tr key={b.id} className="hover:bg-[var(--color-surface)]">
                <td className={`px-3 py-2 whitespace-nowrap ${s.soon ? "text-[var(--color-accent)] font-medium" : "text-[var(--color-text-dim)]"}`}>
                  <div>{s.text}</div>
                  {startAbsolute(b.start_time) && <div className="text-[11px] font-normal text-[var(--color-text-muted)]">{startAbsolute(b.start_time)}</div>}
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-[var(--color-text-dim)]">{gameResolution(b.start_time).label}</td>
                <td className="px-3 py-2 text-[var(--color-text-dim)]">{SPORT_LABEL[b.sport] ?? b.sport}</td>
                <td className="px-3 py-2">
                  <div className="text-[var(--color-text)]">{b.label}</div>
                  <div className="text-[11px] text-[var(--color-text-muted)]">
                    {b.market_type}{b.team ? ` · ${b.team}` : ""}{b.side ? ` ${b.side}` : ""}{b.line != null ? ` ${b.line}` : ""}
                  </div>
                </td>
                <td className="px-3 py-2">
                  <span className="text-[11px] px-1.5 py-0.5 rounded-full border border-[var(--color-border)] text-[var(--color-text-dim)]">
                    {SOURCE_LABEL[b.source] ?? b.source}
                  </span>
                </td>
                <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">
                  {b.market_prob_at_placement != null ? `${(b.market_prob_at_placement * 100).toFixed(0)}%` : "—"}
                </td>
                <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">
                  ${b.stake_dollars.toLocaleString()}{b.stake_units != null ? ` · ${b.stake_units.toFixed(1)}u` : ""}
                </td>
                <td className="px-3 py-2 text-right"><ExplainButton target={reasoningTarget(b)} onExplain={onExplain} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const STATUS_BADGE: Record<string, { label: string; cls: string }> = {
  won: { label: "Won", cls: "bg-[var(--color-good)]/15 text-[var(--color-good)] border-[var(--color-good)]/30" },
  lost: { label: "Lost", cls: "bg-[var(--color-critical)]/15 text-[var(--color-critical)] border-[var(--color-critical)]/30" },
  push: { label: "Push", cls: "bg-[var(--color-border)] text-[var(--color-text-dim)] border-[var(--color-border)]" },
  void: { label: "Void", cls: "bg-[var(--color-border)] text-[var(--color-text-dim)] border-[var(--color-border)]" },
};

function settledDate(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "—";
  return new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function CompletedBets({ bets, onExplain }: { bets: SettledBetPayload[]; onExplain: (t: ReasoningTarget) => void }) {
  if (bets.length === 0) {
    return (
      <div className="text-sm text-[var(--color-text-muted)] border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] px-4 py-6 text-center">
        No completed bets yet. Once a bet settles (auto for game markets, or via the sport's Placed Bets page), it lands here.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto border border-[var(--color-border)] rounded-lg">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--color-text-dim)] border-b border-[var(--color-border)]">
            <th className="px-3 py-2 font-medium">Settled</th>
            <th className="px-3 py-2 font-medium">Sport</th>
            <th className="px-3 py-2 font-medium">Bet</th>
            <th className="px-3 py-2 font-medium">Source</th>
            <th className="px-3 py-2 font-medium">Result</th>
            <th className="px-3 py-2 font-medium">Final score</th>
            <th className="px-3 py-2 font-medium text-right">Entry</th>
            <th className="px-3 py-2 font-medium text-right">Stake</th>
            <th className="px-3 py-2 font-medium text-right">P/L</th>
            <th className="px-3 py-2 font-medium text-right">CLV</th>
            <th className="px-3 py-2 font-medium"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--color-border)]">
          {bets.map((b) => {
            const badge = STATUS_BADGE[b.status] ?? { label: b.status, cls: "border-[var(--color-border)] text-[var(--color-text-dim)]" };
            return (
              <tr key={b.id} className="hover:bg-[var(--color-surface)] align-top">
                <td className="px-3 py-2 whitespace-nowrap text-[var(--color-text-dim)]">{settledDate(b.settled_at)}</td>
                <td className="px-3 py-2 text-[var(--color-text-dim)]">{SPORT_LABEL[b.sport] ?? b.sport}</td>
                <td className="px-3 py-2">
                  <div className="text-[var(--color-text)]">{b.label}</div>
                  <div className="text-[11px] text-[var(--color-text-muted)]">
                    {b.market_type}{b.team ? ` · ${b.team}` : ""}{b.side ? ` ${b.side}` : ""}{b.line != null ? ` ${b.line}` : ""}{b.stake_pool === "futures" ? " · futures" : ""}
                  </div>
                </td>
                <td className="px-3 py-2">
                  <span className="text-[11px] px-1.5 py-0.5 rounded-full border border-[var(--color-border)] text-[var(--color-text-dim)]">
                    {SOURCE_LABEL[b.source] ?? b.source}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <span className={`inline-block text-[11px] px-1.5 py-0.5 rounded-full border ${badge.cls}`}>{badge.label}</span>
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-[var(--color-text-dim)] font-mono text-[12px]">{b.final_score ?? "—"}</td>
                <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">
                  {b.market_prob_at_placement != null ? `${(b.market_prob_at_placement * 100).toFixed(0)}%` : "—"}
                </td>
                <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">
                  ${b.stake_dollars.toLocaleString()}
                </td>
                <td className={`px-3 py-2 text-right font-mono tabular-nums ${b.profit_dollars != null ? pnlClass(b.profit_dollars) : "text-[var(--color-text-dim)]"}`}>
                  {b.profit_dollars != null ? money(b.profit_dollars) : "—"}
                </td>
                <td className={`px-3 py-2 text-right font-mono tabular-nums ${b.clv_status === "closed" && b.clv_pp != null ? pnlClass(b.clv_pp) : "text-[var(--color-text-dim)]"}`}>
                  {b.clv_status === "closed" && b.clv_pp != null ? `${b.clv_pp >= 0 ? "+" : ""}${b.clv_pp.toFixed(1)}pp` : "—"}
                </td>
                <td className="px-3 py-2 text-right"><ExplainButton target={reasoningTarget(b)} onExplain={onExplain} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// Compact list of futures positions (open first, then settled) -- kept lean on
// purpose so the Futures section stays uncluttered: pick, source, status, entry,
// stake, P/L. No CLV column (futures have no clean close).
function FuturesPositions({ open, settled, onExplain }: { open: OpenBetPayload[]; settled: SettledBetPayload[]; onExplain: (t: ReasoningTarget) => void }) {
  if (open.length === 0 && settled.length === 0) return null;
  type FRow = {
    id: number; market_id: number; sport: string; source: string; label: string; market_type: string;
    team: string | null; side: string | null; line: number | null;
    entry: number | null; model_prob: number | null; stake: number; status: string; profit: number | null;
    resolves: string; resolveKey: number;
  };
  const mk = (b: OpenBetPayload | SettledBetPayload, status: string, profit: number | null): FRow => {
    const r = futuresResolution(b.sport, b.market_type);
    return {
      id: b.id, market_id: b.market_id, sport: b.sport, source: b.source, label: b.label, market_type: b.market_type,
      team: b.team, side: b.side, line: b.line, entry: b.market_prob_at_placement, model_prob: b.model_prob_at_placement, stake: b.stake_dollars,
      status, profit, resolves: r.label, resolveKey: r.sortKey,
    };
  };
  // Open first (soonest to resolve → capital frees earliest), then settled.
  // Within each, order by estimated resolution, then sport, then name — so the
  // list reads as an organized "what settles when" schedule instead of
  // placement order.
  const byResolve = (a: FRow, b: FRow) =>
    a.resolveKey - b.resolveKey || a.sport.localeCompare(b.sport) || a.label.localeCompare(b.label);
  const rows: FRow[] = [
    ...open.map((b) => mk(b, "open", null)).sort(byResolve),
    ...settled.map((b) => mk(b, b.status, b.profit_dollars)).sort(byResolve),
  ];
  return (
    <div className="overflow-x-auto border border-[var(--color-border)] rounded-lg mt-2">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--color-text-dim)] border-b border-[var(--color-border)]">
            <th className="px-3 py-2 font-medium">Position</th>
            <th className="px-3 py-2 font-medium">Source</th>
            <th className="px-3 py-2 font-medium">Status</th>
            <th className="px-3 py-2 font-medium">Est. resolves</th>
            <th className="px-3 py-2 font-medium text-right">Entry</th>
            <th className="px-3 py-2 font-medium text-right">Stake</th>
            <th className="px-3 py-2 font-medium text-right">P/L</th>
            <th className="px-3 py-2 font-medium"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--color-border)]">
          {rows.map((r) => {
            const badge = r.status === "open"
              ? { label: "Open", cls: "bg-[var(--color-accent)]/15 text-[var(--color-accent)] border-[var(--color-accent)]/30" }
              : (STATUS_BADGE[r.status] ?? { label: r.status, cls: "border-[var(--color-border)] text-[var(--color-text-dim)]" });
            return (
              <tr key={`${r.status}-${r.id}`} className="hover:bg-[var(--color-surface)] align-top">
                <td className="px-3 py-2">
                  <div className="text-[var(--color-text)]">
                    {r.team ?? r.side ?? "—"}
                    {futuresThreshold({ market_type: r.market_type, side: r.side, line: r.line }) && (
                      <span className="text-[var(--color-text-dim)]"> · {futuresThreshold({ market_type: r.market_type, side: r.side, line: r.line })}</span>
                    )}
                  </div>
                  <div className="text-[11px] text-[var(--color-text-muted)]">
                    {futuresMarketName({ market_type: r.market_type, side: r.side, line: r.line, group_label: r.label })} · {SPORT_LABEL[r.sport] ?? r.sport}
                  </div>
                </td>
                <td className="px-3 py-2">
                  <span className="text-[11px] px-1.5 py-0.5 rounded-full border border-[var(--color-border)] text-[var(--color-text-dim)]">
                    {SOURCE_LABEL[r.source] ?? r.source}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <span className={`inline-block text-[11px] px-1.5 py-0.5 rounded-full border ${badge.cls}`}>{badge.label}</span>
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-[var(--color-text-dim)]">{r.resolves}</td>
                <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">
                  {r.entry != null ? `${(r.entry * 100).toFixed(0)}%` : "—"}
                </td>
                <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">${r.stake.toLocaleString()}</td>
                <td className={`px-3 py-2 text-right font-mono tabular-nums ${r.profit != null ? pnlClass(r.profit) : "text-[var(--color-text-dim)]"}`}>
                  {r.profit != null ? money(r.profit) : "—"}
                </td>
                <td className="px-3 py-2 text-right">
                  <ExplainButton target={reasoningTarget({ market_id: r.market_id, sport: r.sport, model_prob_at_placement: r.model_prob, market_prob_at_placement: r.entry })} onExplain={onExplain} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// Clickable section header that collapses/expands its body (chevron toggle).
function CollapsibleHeader({ title, sub, collapsed, onToggle }: { title: string; sub?: string; collapsed: boolean; onToggle: () => void }) {
  const Chevron = collapsed ? ChevronRight : ChevronDown;
  return (
    <button onClick={onToggle} className="flex items-center gap-1.5 mb-2 mt-8 group" aria-expanded={!collapsed}>
      <Chevron size={15} className="text-[var(--color-text-muted)] group-hover:text-[var(--color-text)] shrink-0" />
      <span className="text-sm font-medium">{title}</span>
      {sub && <span className="text-[11px] text-[var(--color-text-muted)]">{sub}</span>}
    </button>
  );
}

export function Tracker() {
  const { data, isLoading, isError } = useQuery({ queryKey: ["portfolio"], queryFn: fetchPortfolio });
  const openQuery = useQuery({ queryKey: ["open-bets"], queryFn: fetchOpenBets });
  const settledQuery = useQuery({ queryKey: ["settled-bets"], queryFn: fetchSettledBets });
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const unitDollars = settingsQuery.data?.unit_dollars ?? 0;
  const [curveMode, setCurveMode] = useState<CurveMode>("dollars");
  const [futuresCollapsed, setFuturesCollapsed] = useState(false);
  const [completedCollapsed, setCompletedCollapsed] = useState(false);
  const [reasoning, setReasoning] = useState<ReasoningTarget | null>(null);

  const decided = (data?.wins ?? 0) + (data?.losses ?? 0);
  const winRate = decided > 0 ? (data!.wins / decided) * 100 : null;
  const unitsLabel = data && unitDollars > 0 ? ` (${units(data.net_units)})` : "";
  // Split game vs futures so each lands only in its own section (a futures bet
  // has no kickoff, so it shouldn't sit in the game "Open positions" watchlist
  // or "Completed bets" list -- it gets its own compact list under Futures).
  const allOpen = openQuery.data ?? [];
  const allSettled = settledQuery.data ?? [];
  const openBets = allOpen.filter((b) => b.stake_pool !== "futures");
  const settledBets = allSettled.filter((b) => b.stake_pool !== "futures");
  const futuresOpen = allOpen.filter((b) => b.stake_pool === "futures");
  const futuresSettled = allSettled.filter((b) => b.stake_pool === "futures");

  return (
    <PageShell title="Bet Tracker">
      {isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}

      {isLoading || !data ? (
        <>
          <StatTilesSkeleton count={4} />
          <TableSkeleton cols={8} />
        </>
      ) : (
        <>
          <div className="flex flex-wrap border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] mb-4 divide-x divide-[var(--color-border)]">
            <StatTile
              label="Net P/L"
              value={`${money(data.net_profit_dollars)}${unitsLabel}`}
              sublabel="game bets, realized at placement price (futures below)"
            />
            <StatTile
              label="ROI"
              value={data.roi !== null ? `${(data.roi * 100).toFixed(1)}%` : "—"}
              sublabel={data.roi !== null ? `on $${data.staked_dollars.toLocaleString()} staked (decided bets)` : "no settled bets yet"}
            />
            <StatTile
              label="Record"
              value={`${data.wins}–${data.losses}${data.pushes + data.voids > 0 ? ` · ${data.pushes + data.voids}p/v` : ""}`}
              sublabel={winRate !== null ? `${winRate.toFixed(0)}% win rate` : "no decided bets yet"}
            />
            <StatTile
              label="Avg CLV"
              value={data.avg_clv_pp !== null ? `${data.avg_clv_pp >= 0 ? "+" : ""}${data.avg_clv_pp.toFixed(1)}pp` : "—"}
              sublabel={`${data.clv_sample} bet${data.clv_sample === 1 ? "" : "s"} with a real close`}
            />
          </div>

          {(data.pending > 0 || data.at_risk_dollars > 0) && (
            <div className="text-xs text-[var(--color-text-muted)] mb-4">
              <span className="text-[var(--color-text-dim)]">{data.pending}</span> open position{data.pending === 1 ? "" : "s"} ·{" "}
              <span className="text-[var(--color-text-dim)]">${data.at_risk_dollars.toLocaleString()}</span> at risk (not yet settled, excluded from P/L and ROI above)
            </div>
          )}

          {/* Open positions -- the "what's coming up that I've bet on" watchlist */}
          <div className="text-sm font-medium mb-2">Open positions {openBets.length > 0 && <span className="text-[var(--color-text-muted)] font-normal">— soonest first</span>}</div>
          <OpenPositions bets={openBets} onExplain={setReasoning} />
          {futuresOpen.length > 0 && (
            <div className="text-xs text-[var(--color-text-muted)] mt-2">
              {openBets.length === 0 ? "No game bets open, but you have " : "Plus "}
              <span className="text-[var(--color-text-dim)]">{futuresOpen.length} futures position{futuresOpen.length === 1 ? "" : "s"}</span>
              {" "}(${futuresOpen.reduce((s, b) => s + b.stake_dollars, 0).toLocaleString()} at risk) in the <span className="text-[var(--color-text-dim)]">Futures</span> section below.
            </div>
          )}

          <div className="flex items-center justify-between mb-2 mt-8">
            <div className="text-sm font-medium">Cumulative P/L</div>
            <div className="flex items-center gap-1 text-xs">
              {(["dollars", "units"] as CurveMode[]).map((m) => (
                <button
                  key={m}
                  onClick={() => setCurveMode(m)}
                  className={
                    curveMode === m
                      ? "px-2 py-0.5 rounded-md bg-[var(--color-accent)] text-[#1c1408] font-medium"
                      : "px-2 py-0.5 rounded-md border border-[var(--color-border)] text-[var(--color-text-dim)] hover:text-[var(--color-text)]"
                  }
                >
                  {m === "dollars" ? "$" : "Units"}
                </button>
              ))}
            </div>
          </div>
          <EquityCurve points={data.equity_curve} mode={curveMode} />

          {/* Kalshi vs Polymarket -- quick side-by-side, as asked */}
          {data.by_source.length > 0 && (
            <>
              <div className="text-sm font-medium mb-2 mt-8">By platform</div>
              <div className="overflow-x-auto border border-[var(--color-border)] rounded-lg">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--color-text-dim)] border-b border-[var(--color-border)]">
                      <th className="px-3 py-2 font-medium">Platform</th>
                      <th className="px-3 py-2 font-medium text-right">Net P/L</th>
                      <th className="px-3 py-2 font-medium text-right">ROI</th>
                      <th className="px-3 py-2 font-medium text-right">Record</th>
                      <th className="px-3 py-2 font-medium text-right">Staked</th>
                      <th className="px-3 py-2 font-medium text-right">Open</th>
                      <th className="px-3 py-2 font-medium text-right">Avg CLV</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--color-border)]">
                    {data.by_source.map((s) => (
                      <tr key={s.source} className="hover:bg-[var(--color-surface)]">
                        <td className="px-3 py-2 font-medium">{SOURCE_LABEL[s.source] ?? s.source}</td>
                        <td className={`px-3 py-2 text-right font-mono tabular-nums ${pnlClass(s.net_profit_dollars)}`}>{money(s.net_profit_dollars)}</td>
                        <td className={`px-3 py-2 text-right font-mono tabular-nums ${s.roi !== null ? pnlClass(s.roi) : "text-[var(--color-text-dim)]"}`}>
                          {s.roi !== null ? `${(s.roi * 100).toFixed(1)}%` : "—"}
                        </td>
                        <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">{s.wins}–{s.losses}</td>
                        <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">${s.staked_dollars.toLocaleString()}</td>
                        <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">{s.pending > 0 ? `${s.pending} · $${s.at_risk_dollars.toLocaleString()}` : "—"}</td>
                        <td className={`px-3 py-2 text-right font-mono tabular-nums ${s.avg_clv_pp !== null ? pnlClass(s.avg_clv_pp) : "text-[var(--color-text-dim)]"}`}>
                          {s.avg_clv_pp !== null ? `${s.avg_clv_pp >= 0 ? "+" : ""}${s.avg_clv_pp.toFixed(1)}pp` : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {/* Futures -- kept separate: season-long, no clean CLV close, capital
              tied up for months, so their P/L is NOT blended into the game
              numbers above. */}
          {(data.futures.pending > 0 || data.futures.wins + data.futures.losses + data.futures.pushes + data.futures.voids > 0) && (
            <>
              <CollapsibleHeader
                title="Futures"
                sub={futuresCollapsed ? `${data.futures.pending} open · ${money(data.futures.net_profit_dollars)}` : "season-long / tournament — tracked separately, no CLV"}
                collapsed={futuresCollapsed}
                onToggle={() => setFuturesCollapsed((v) => !v)}
              />
              {!futuresCollapsed && (<>
              <div className="flex flex-wrap border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] mb-2 divide-x divide-[var(--color-border)]">
                <StatTile
                  label="Futures P/L"
                  value={money(data.futures.net_profit_dollars)}
                  sublabel={data.futures.roi !== null ? `${(data.futures.roi * 100).toFixed(1)}% ROI on $${data.futures.staked_dollars.toLocaleString()} settled` : "nothing settled yet"}
                />
                <StatTile
                  label="Futures record"
                  value={`${data.futures.wins}–${data.futures.losses}`}
                  sublabel={`${data.futures.pending} open`}
                />
                <StatTile
                  label="Futures at risk"
                  value={`$${data.futures.at_risk_dollars.toLocaleString()}`}
                  sublabel="capital locked until these resolve"
                />
              </div>
              {data.futures.by_sport.length > 0 && (
                <div className="overflow-x-auto border border-[var(--color-border)] rounded-lg">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--color-text-dim)] border-b border-[var(--color-border)]">
                        <th className="px-3 py-2 font-medium">Sport</th>
                        <th className="px-3 py-2 font-medium text-right">Net P/L</th>
                        <th className="px-3 py-2 font-medium text-right">Record</th>
                        <th className="px-3 py-2 font-medium text-right">Staked</th>
                        <th className="px-3 py-2 font-medium text-right">Open</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[var(--color-border)]">
                      {data.futures.by_sport.map((s) => (
                        <tr key={s.sport} className="hover:bg-[var(--color-surface)]">
                          <td className="px-3 py-2 font-medium">{SPORT_LABEL[s.sport] ?? s.sport}</td>
                          <td className={`px-3 py-2 text-right font-mono tabular-nums ${pnlClass(s.net_profit_dollars)}`}>{money(s.net_profit_dollars)}</td>
                          <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">{s.wins}–{s.losses}</td>
                          <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">${s.staked_dollars.toLocaleString()}</td>
                          <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">{s.pending > 0 ? `${s.pending} · $${s.at_risk_dollars.toLocaleString()}` : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <FuturesPositions open={futuresOpen} settled={futuresSettled} onExplain={setReasoning} />
              </>)}
            </>
          )}

          <div className="flex items-baseline gap-2 mb-2 mt-8">
            <div className="text-sm font-medium">By sport</div>
            <div className="text-[11px] text-[var(--color-text-muted)]">game bets</div>
          </div>
          <div className="overflow-x-auto border border-[var(--color-border)] rounded-lg">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--color-text-dim)] border-b border-[var(--color-border)]">
                  <th className="px-3 py-2 font-medium">Sport</th>
                  <th className="px-3 py-2 font-medium text-right">Net P/L</th>
                  <th className="px-3 py-2 font-medium text-right">ROI</th>
                  <th className="px-3 py-2 font-medium text-right">Record</th>
                  <th className="px-3 py-2 font-medium text-right">Staked</th>
                  <th className="px-3 py-2 font-medium text-right">Open</th>
                  <th className="px-3 py-2 font-medium text-right">Avg CLV</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--color-border)]">
                {data.by_sport.length === 0 && (
                  <tr>
                    <td colSpan={7} className="px-3 py-6 text-center text-[var(--color-text-muted)]">
                      No bets tracked yet. Mark a recommended bet as placed and it shows up here.
                    </td>
                  </tr>
                )}
                {data.by_sport.map((s) => (
                  <tr key={s.sport} className="hover:bg-[var(--color-surface)]">
                    <td className="px-3 py-2 font-medium">{SPORT_LABEL[s.sport] ?? s.sport}</td>
                    <td className={`px-3 py-2 text-right font-mono tabular-nums ${pnlClass(s.net_profit_dollars)}`}>
                      {money(s.net_profit_dollars)}
                    </td>
                    <td className={`px-3 py-2 text-right font-mono tabular-nums ${s.roi !== null ? pnlClass(s.roi) : "text-[var(--color-text-dim)]"}`}>
                      {s.roi !== null ? `${(s.roi * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">
                      {s.wins}–{s.losses}{s.pushes + s.voids > 0 ? ` · ${s.pushes + s.voids}` : ""}
                    </td>
                    <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">
                      ${s.staked_dollars.toLocaleString()}
                    </td>
                    <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-text-dim)]">
                      {s.pending > 0 ? `${s.pending} · $${s.at_risk_dollars.toLocaleString()}` : "—"}
                    </td>
                    <td className={`px-3 py-2 text-right font-mono tabular-nums ${s.avg_clv_pp !== null ? pnlClass(s.avg_clv_pp) : "text-[var(--color-text-dim)]"}`}>
                      {s.avg_clv_pp !== null ? `${s.avg_clv_pp >= 0 ? "+" : ""}${s.avg_clv_pp.toFixed(1)}pp` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Completed bets -- the settled history */}
          <CollapsibleHeader
            title="Completed bets"
            sub={settledBets.length > 0 ? `${settledBets.length} settled${completedCollapsed ? "" : " — most recent first"}` : undefined}
            collapsed={completedCollapsed}
            onToggle={() => setCompletedCollapsed((v) => !v)}
          />
          {!completedCollapsed && <CompletedBets bets={settledBets} onExplain={setReasoning} />}
        </>
      )}

      {reasoning && (
        <BetReasoningModal
          marketId={reasoning.marketId}
          modelProb={reasoning.modelProb}
          marketProb={reasoning.marketProb}
          sport={reasoning.sport}
          onClose={() => setReasoning(null)}
        />
      )}

      <p className="text-xs text-[var(--color-text-muted)] mt-4 max-w-2xl">
        Every bet you mark placed is snapshotted at that moment (entry price, stake, model edge), then
        settled won/lost/push/void — automatically for game-tied markets once the final score lands, or by
        hand on each sport's Placed Bets page for everything else. P/L is realized on settled bets only:
        a win profits <span className="font-mono">stake × (1−price)/price</span> at the price you got, a loss
        forfeits the stake, and pushes/voids return it. ROI divides net P/L by stake on decided bets. CLV
        (closing-line value) shows whether you beat the market's own closing price — the one signal that
        holds up on a small sample, since none of these models is validated to beat the market on average.
        {" "}"Est. resolves" is a rough guide to when a futures position settles (and its capital frees) —
        estimated from the normal league calendar (e.g. NFL regular season ~Jan, MLB ~late Sep, European
        soccer ~May), not a precise date.
      </p>
    </PageShell>
  );
}
