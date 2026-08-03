import { useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { PageShell } from "../components/layout/PageShell";
import {
  fetchSettings, updateSettings, perGamePoolLabel,
  fetchAlertConfig, updateAlertConfig,
} from "../api/markets";
import type { SportKey } from "../lib/sports";

const NUM = "w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-sm tabular-nums focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]";
const CELL_NUM = "w-16 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 text-sm tabular-nums text-right focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]";

// Which SettingsPayload fields hold each sport's derived pools (total, per-game,
// futures). NFL is the odd one out -- its per-game/futures fields are unprefixed.
const POOL_FIELDS: Record<string, [string, string, string]> = {
  nfl: ["nfl_pool_dollars", "weekly_pool_dollars", "futures_pool_dollars"],
  nba: ["nba_pool_dollars", "nba_weekly_pool_dollars", "nba_futures_pool_dollars"],
  wnba: ["wnba_pool_dollars", "wnba_weekly_pool_dollars", "wnba_futures_pool_dollars"],
  mlb: ["mlb_pool_dollars", "mlb_weekly_pool_dollars", "mlb_futures_pool_dollars"],
  mma: ["mma_pool_dollars", "mma_weekly_pool_dollars", "mma_futures_pool_dollars"],
  tennis: ["tennis_pool_dollars", "tennis_weekly_pool_dollars", "tennis_futures_pool_dollars"],
  soccer: ["soccer_pool_dollars", "soccer_weekly_pool_dollars", "soccer_futures_pool_dollars"],
  valorant: ["valorant_pool_dollars", "valorant_weekly_pool_dollars", "valorant_futures_pool_dollars"],
  cs2: ["cs2_pool_dollars", "cs2_weekly_pool_dollars", "cs2_futures_pool_dollars"],
  lol: ["lol_pool_dollars", "lol_weekly_pool_dollars", "lol_futures_pool_dollars"],
};

function SectionCard({ title, sub, children }: { title: string; sub?: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-5 py-5">
      <div className="text-sm font-semibold text-[var(--color-text)]">{title}</div>
      {sub && <div className="text-xs text-[var(--color-text-dim)] mt-1 mb-4 max-w-2xl leading-relaxed">{sub}</div>}
      {!sub && <div className="mb-4" />}
      {children}
    </div>
  );
}

// Discord alert config -- self-contained (own query/state) so it doesn't touch
// the big settings form. The webhook URL is write-only (backend never echoes it,
// it's a secret) -- we only show whether one is configured.
function AlertsSection() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["alert-config"], queryFn: fetchAlertConfig });
  const [url, setUrl] = useState("");
  const [minEdge, setMinEdge] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => { if (q.data && minEdge === "") setMinEdge(String(Math.round(q.data.min_edge_pp * 100))); }, [q.data]); // eslint-disable-line
  async function save() {
    const body: { webhook_url?: string; min_edge_pp?: number } = {};
    if (url.trim()) body.webhook_url = url.trim();
    if (minEdge !== "") body.min_edge_pp = Number(minEdge) / 100;
    await updateAlertConfig(body);
    setUrl(""); setSaved(true); setTimeout(() => setSaved(false), 2500);
    qc.invalidateQueries({ queryKey: ["alert-config"] });
  }
  return (
    <SectionCard
      title="🔔 New-bet alerts (Discord)"
      sub="Get a Discord message on your phone the moment a NEW bet clears your guidelines — even when the app is closed. In Discord: Server Settings → Integrations → Webhooks → New Webhook → Copy URL, then paste it below. Only genuinely new bets above the edge floor fire (batched, deduped — no spam)."
    >
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 items-end">
        <div className="md:col-span-2">
          <label className="text-xs text-[var(--color-text-dim)]">
            Discord webhook URL {q.data?.webhook_configured && <span className="text-[var(--color-good)]">· configured ✓</span>}
          </label>
          <input type="password" value={url} onChange={(e) => setUrl(e.target.value)}
            placeholder={q.data?.webhook_configured ? "•••••• (paste to replace)" : "https://discord.com/api/webhooks/…"}
            className={`mt-1 ${NUM}`} />
        </div>
        <div>
          <label className="text-xs text-[var(--color-text-dim)]">Alert edge floor (pp)</label>
          <input type="number" min="0" max="100" step="1" value={minEdge} onChange={(e) => setMinEdge(e.target.value)} className={`mt-1 ${NUM}`} />
        </div>
      </div>
      <div className="mt-4 flex items-center gap-3">
        <button onClick={save} className="text-sm font-medium px-3 py-1.5 rounded-md border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)]/10">Save alerts</button>
        {saved && <span className="text-xs text-[var(--color-good)]">Saved ✓</span>}
        {q.data?.webhook_configured && (
          <button onClick={async () => { await updateAlertConfig({ webhook_url: "" }); qc.invalidateQueries({ queryKey: ["alert-config"] }); }}
            className="text-xs text-[var(--color-text-muted)] hover:text-[var(--color-critical)]">disable</button>
        )}
      </div>
    </SectionCard>
  );
}

export function Settings() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: ["settings"],
    queryFn: fetchSettings,
  });

  const [bankrollInput, setBankrollInput] = useState("");
  const [unitsInput, setUnitsInput] = useState("");
  const [nflAllocInput, setNflAllocInput] = useState(""); // as a whole-number percent, e.g. "15"
  const [futuresSubpoolInput, setFuturesSubpoolInput] = useState(""); // as a whole-number percent, e.g. "30"
  const [nbaAllocInput, setNbaAllocInput] = useState(""); // as a whole-number percent, e.g. "15"
  const [nbaFuturesSubpoolInput, setNbaFuturesSubpoolInput] = useState(""); // as a whole-number percent, e.g. "30"
  const [wnbaAllocInput, setWnbaAllocInput] = useState(""); // as a whole-number percent, e.g. "15"
  const [wnbaFuturesSubpoolInput, setWnbaFuturesSubpoolInput] = useState(""); // whole-number percent; 0 (WNBA is moneyline-only, no futures)
  const [mlbAllocInput, setMlbAllocInput] = useState(""); // as a whole-number percent, e.g. "15"
  const [mlbFuturesSubpoolInput, setMlbFuturesSubpoolInput] = useState(""); // as a whole-number percent, e.g. "30"
  const [mmaAllocInput, setMmaAllocInput] = useState(""); // as a whole-number percent, e.g. "15"
  const [mmaFuturesSubpoolInput, setMmaFuturesSubpoolInput] = useState(""); // as a whole-number percent, e.g. "15" -- lighter than other sports, KXUFCTITLE is thin/illiquid
  const [tennisAllocInput, setTennisAllocInput] = useState(""); // as a whole-number percent, e.g. "15"
  const [tennisFuturesSubpoolInput, setTennisFuturesSubpoolInput] = useState(""); // as a whole-number percent, e.g. "15" -- tournament-winner futures, same "thin relative to per-match volume" reasoning as MMA's own 15%
  const [soccerAllocInput, setSoccerAllocInput] = useState(""); // as a whole-number percent, e.g. "15"
  const [soccerFuturesSubpoolInput, setSoccerFuturesSubpoolInput] = useState(""); // as a whole-number percent, e.g. "15" -- no Soccer futures ingested yet, same "thin/none built" reasoning as Tennis's own default
  // Valorant/CS2/LoL each get their OWN independent 15%/15% slot as of
  // 2026-07-20 (explicit user request -- "treat esports as different
  // leagues like we do with other sports") -- previously all 3 shared ONE
  // esports slot, never exposed in this UI at all.
  const [valorantAllocInput, setValorantAllocInput] = useState(""); // as a whole-number percent, e.g. "15"
  const [valorantFuturesSubpoolInput, setValorantFuturesSubpoolInput] = useState(""); // as a whole-number percent, e.g. "15"
  const [cs2AllocInput, setCs2AllocInput] = useState(""); // as a whole-number percent, e.g. "15"
  const [cs2FuturesSubpoolInput, setCs2FuturesSubpoolInput] = useState(""); // as a whole-number percent, e.g. "15"
  const [lolAllocInput, setLolAllocInput] = useState(""); // as a whole-number percent, e.g. "15"
  const [lolFuturesSubpoolInput, setLolFuturesSubpoolInput] = useState(""); // as a whole-number percent, e.g. "15"
  const [fractionalKellyInput, setFractionalKellyInput] = useState(""); // whole-number percent, e.g. "25"
  const [maxStakeInput, setMaxStakeInput] = useState(""); // whole-number percent, e.g. "5"
  const [minEdgeInput, setMinEdgeInput] = useState(""); // whole-number percentage points, e.g. "3"
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");

  useEffect(() => {
    if (data) {
      setBankrollInput(String(data.bankroll_dollars));
      setUnitsInput(String(data.bankroll_units));
      setNflAllocInput(String(Math.round(data.nfl_allocation_pct * 100)));
      setFuturesSubpoolInput(String(Math.round(data.futures_subpool_pct * 100)));
      setNbaAllocInput(String(Math.round(data.nba_allocation_pct * 100)));
      setNbaFuturesSubpoolInput(String(Math.round(data.nba_futures_subpool_pct * 100)));
      setWnbaAllocInput(String(Math.round(data.wnba_allocation_pct * 100)));
      setWnbaFuturesSubpoolInput(String(Math.round(data.wnba_futures_subpool_pct * 100)));
      setMlbAllocInput(String(Math.round(data.mlb_allocation_pct * 100)));
      setMlbFuturesSubpoolInput(String(Math.round(data.mlb_futures_subpool_pct * 100)));
      setMmaAllocInput(String(Math.round(data.mma_allocation_pct * 100)));
      setMmaFuturesSubpoolInput(String(Math.round(data.mma_futures_subpool_pct * 100)));
      setTennisAllocInput(String(Math.round(data.tennis_allocation_pct * 100)));
      setTennisFuturesSubpoolInput(String(Math.round(data.tennis_futures_subpool_pct * 100)));
      setSoccerAllocInput(String(Math.round(data.soccer_allocation_pct * 100)));
      setSoccerFuturesSubpoolInput(String(Math.round(data.soccer_futures_subpool_pct * 100)));
      setValorantAllocInput(String(Math.round(data.valorant_allocation_pct * 100)));
      setValorantFuturesSubpoolInput(String(Math.round(data.valorant_futures_subpool_pct * 100)));
      setCs2AllocInput(String(Math.round(data.cs2_allocation_pct * 100)));
      setCs2FuturesSubpoolInput(String(Math.round(data.cs2_futures_subpool_pct * 100)));
      setLolAllocInput(String(Math.round(data.lol_allocation_pct * 100)));
      setLolFuturesSubpoolInput(String(Math.round(data.lol_futures_subpool_pct * 100)));
      setFractionalKellyInput(String(Math.round(data.fractional_kelly * 100)));
      setMaxStakeInput(String(Math.round(data.max_stake_fraction * 100)));
      setMinEdgeInput(String(Math.round(data.min_edge_to_bet * 100)));
    }
  }, [data]);

  async function handleSave() {
    const bankroll = Number(bankrollInput);
    const units = Number(unitsInput);
    const nflAllocPct = Number(nflAllocInput) / 100;
    const futuresSubpoolPct = Number(futuresSubpoolInput) / 100;
    const nbaAllocPct = Number(nbaAllocInput) / 100;
    const nbaFuturesSubpoolPct = Number(nbaFuturesSubpoolInput) / 100;
    const wnbaAllocPct = Number(wnbaAllocInput) / 100;
    const wnbaFuturesSubpoolPct = Number(wnbaFuturesSubpoolInput) / 100;
    const mlbAllocPct = Number(mlbAllocInput) / 100;
    const mlbFuturesSubpoolPct = Number(mlbFuturesSubpoolInput) / 100;
    const mmaAllocPct = Number(mmaAllocInput) / 100;
    const mmaFuturesSubpoolPct = Number(mmaFuturesSubpoolInput) / 100;
    const tennisAllocPct = Number(tennisAllocInput) / 100;
    const tennisFuturesSubpoolPct = Number(tennisFuturesSubpoolInput) / 100;
    const soccerAllocPct = Number(soccerAllocInput) / 100;
    const soccerFuturesSubpoolPct = Number(soccerFuturesSubpoolInput) / 100;
    const valorantAllocPct = Number(valorantAllocInput) / 100;
    const valorantFuturesSubpoolPct = Number(valorantFuturesSubpoolInput) / 100;
    const cs2AllocPct = Number(cs2AllocInput) / 100;
    const cs2FuturesSubpoolPct = Number(cs2FuturesSubpoolInput) / 100;
    const lolAllocPct = Number(lolAllocInput) / 100;
    const lolFuturesSubpoolPct = Number(lolFuturesSubpoolInput) / 100;
    const fractionalKelly = Number(fractionalKellyInput) / 100;
    const maxStakeFraction = Number(maxStakeInput) / 100;
    const minEdgeToBet = Number(minEdgeInput) / 100;
    const valid =
      Number.isFinite(bankroll) && bankroll > 0 &&
      Number.isFinite(units) && units > 0 &&
      Number.isFinite(nflAllocPct) && nflAllocPct > 0 && nflAllocPct <= 1 &&
      Number.isFinite(futuresSubpoolPct) && futuresSubpoolPct >= 0 && futuresSubpoolPct <= 1 &&
      Number.isFinite(nbaAllocPct) && nbaAllocPct > 0 && nbaAllocPct <= 1 &&
      Number.isFinite(nbaFuturesSubpoolPct) && nbaFuturesSubpoolPct >= 0 && nbaFuturesSubpoolPct <= 1 &&
      Number.isFinite(wnbaAllocPct) && wnbaAllocPct > 0 && wnbaAllocPct <= 1 &&
      Number.isFinite(wnbaFuturesSubpoolPct) && wnbaFuturesSubpoolPct >= 0 && wnbaFuturesSubpoolPct <= 1 &&
      Number.isFinite(mlbAllocPct) && mlbAllocPct > 0 && mlbAllocPct <= 1 &&
      Number.isFinite(mlbFuturesSubpoolPct) && mlbFuturesSubpoolPct >= 0 && mlbFuturesSubpoolPct <= 1 &&
      Number.isFinite(mmaAllocPct) && mmaAllocPct > 0 && mmaAllocPct <= 1 &&
      Number.isFinite(mmaFuturesSubpoolPct) && mmaFuturesSubpoolPct >= 0 && mmaFuturesSubpoolPct <= 1 &&
      Number.isFinite(tennisAllocPct) && tennisAllocPct > 0 && tennisAllocPct <= 1 &&
      Number.isFinite(tennisFuturesSubpoolPct) && tennisFuturesSubpoolPct >= 0 && tennisFuturesSubpoolPct <= 1 &&
      Number.isFinite(soccerAllocPct) && soccerAllocPct > 0 && soccerAllocPct <= 1 &&
      Number.isFinite(soccerFuturesSubpoolPct) && soccerFuturesSubpoolPct >= 0 && soccerFuturesSubpoolPct <= 1 &&
      Number.isFinite(valorantAllocPct) && valorantAllocPct > 0 && valorantAllocPct <= 1 &&
      Number.isFinite(valorantFuturesSubpoolPct) && valorantFuturesSubpoolPct >= 0 && valorantFuturesSubpoolPct <= 1 &&
      Number.isFinite(cs2AllocPct) && cs2AllocPct > 0 && cs2AllocPct <= 1 &&
      Number.isFinite(cs2FuturesSubpoolPct) && cs2FuturesSubpoolPct >= 0 && cs2FuturesSubpoolPct <= 1 &&
      Number.isFinite(lolAllocPct) && lolAllocPct > 0 && lolAllocPct <= 1 &&
      Number.isFinite(lolFuturesSubpoolPct) && lolFuturesSubpoolPct >= 0 && lolFuturesSubpoolPct <= 1 &&
      Number.isFinite(fractionalKelly) && fractionalKelly > 0 && fractionalKelly <= 1 &&
      Number.isFinite(maxStakeFraction) && maxStakeFraction > 0 && maxStakeFraction <= 1 &&
      Number.isFinite(minEdgeToBet) && minEdgeToBet >= 0 && minEdgeToBet <= 1;
    if (!valid) {
      setSaveState("error");
      return;
    }
    setSaveState("saving");
    try {
      const updated = await updateSettings({
        bankrollDollars: bankroll,
        bankrollUnits: units,
        nflAllocationPct: nflAllocPct,
        futuresSubpoolPct: futuresSubpoolPct,
        nbaAllocationPct: nbaAllocPct,
        nbaFuturesSubpoolPct: nbaFuturesSubpoolPct,
        wnbaAllocationPct: wnbaAllocPct,
        wnbaFuturesSubpoolPct: wnbaFuturesSubpoolPct,
        mlbAllocationPct: mlbAllocPct,
        mlbFuturesSubpoolPct: mlbFuturesSubpoolPct,
        mmaAllocationPct: mmaAllocPct,
        mmaFuturesSubpoolPct: mmaFuturesSubpoolPct,
        tennisAllocationPct: tennisAllocPct,
        tennisFuturesSubpoolPct: tennisFuturesSubpoolPct,
        soccerAllocationPct: soccerAllocPct,
        soccerFuturesSubpoolPct: soccerFuturesSubpoolPct,
        valorantAllocationPct: valorantAllocPct,
        valorantFuturesSubpoolPct: valorantFuturesSubpoolPct,
        cs2AllocationPct: cs2AllocPct,
        cs2FuturesSubpoolPct: cs2FuturesSubpoolPct,
        lolAllocationPct: lolAllocPct,
        lolFuturesSubpoolPct: lolFuturesSubpoolPct,
        fractionalKelly,
        maxStakeFraction,
        minEdgeToBet,
      });
      queryClient.setQueryData(["settings"], updated);
      // Pool sizing AND the staking thresholds both drive kelly_fraction/
      // suggested_stake_dollars/units on every market row -- refetch every
      // sport's tables so displayed stakes/edges reflect the new numbers
      // immediately.
      queryClient.invalidateQueries({ queryKey: ["markets"] });
      queryClient.invalidateQueries({ queryKey: ["futures"] });
      queryClient.invalidateQueries({ queryKey: ["nba"] });
      queryClient.invalidateQueries({ queryKey: ["mlb"] });
      queryClient.invalidateQueries({ queryKey: ["mma"] });
      queryClient.invalidateQueries({ queryKey: ["tennis"] });
      queryClient.invalidateQueries({ queryKey: ["soccer"] });
      queryClient.invalidateQueries({ queryKey: ["valorant"] });
      queryClient.invalidateQueries({ queryKey: ["cs2"] });
      queryClient.invalidateQueries({ queryKey: ["lol"] });
      setSaveState("saved");
    } catch {
      setSaveState("error");
    }
  }

  const idle = () => setSaveState("idle");
  const unitStr = (n: number) => (data && data.unit_dollars > 0 ? `${(n / data.unit_dollars).toFixed(1)}u` : "");
  const poolVal = (key: string, i: number): number | null => {
    if (!data) return null;
    const f = POOL_FIELDS[key]?.[i];
    return f ? ((data as unknown as Record<string, number>)[f] ?? null) : null;
  };

  // Per-sport allocation rows, config-driven so the 10 sports render as one
  // clean table (alloc %, futures %, and the resulting $ pools inline) instead
  // of 20 stacked inputs plus a separate wall of pool read-outs.
  const sportRows: { key: SportKey; label: string; alloc: string; setAlloc: (v: string) => void; fut: string; setFut: (v: string) => void; noFutures?: boolean }[] = [
    { key: "nfl", label: "NFL", alloc: nflAllocInput, setAlloc: setNflAllocInput, fut: futuresSubpoolInput, setFut: setFuturesSubpoolInput },
    { key: "nba", label: "NBA", alloc: nbaAllocInput, setAlloc: setNbaAllocInput, fut: nbaFuturesSubpoolInput, setFut: setNbaFuturesSubpoolInput },
    { key: "wnba", label: "WNBA", alloc: wnbaAllocInput, setAlloc: setWnbaAllocInput, fut: wnbaFuturesSubpoolInput, setFut: setWnbaFuturesSubpoolInput, noFutures: true },
    { key: "mlb", label: "MLB", alloc: mlbAllocInput, setAlloc: setMlbAllocInput, fut: mlbFuturesSubpoolInput, setFut: setMlbFuturesSubpoolInput },
    { key: "mma", label: "MMA", alloc: mmaAllocInput, setAlloc: setMmaAllocInput, fut: mmaFuturesSubpoolInput, setFut: setMmaFuturesSubpoolInput },
    { key: "tennis", label: "Tennis", alloc: tennisAllocInput, setAlloc: setTennisAllocInput, fut: tennisFuturesSubpoolInput, setFut: setTennisFuturesSubpoolInput },
    { key: "soccer", label: "Soccer", alloc: soccerAllocInput, setAlloc: setSoccerAllocInput, fut: soccerFuturesSubpoolInput, setFut: setSoccerFuturesSubpoolInput },
    { key: "valorant", label: "Valorant", alloc: valorantAllocInput, setAlloc: setValorantAllocInput, fut: valorantFuturesSubpoolInput, setFut: setValorantFuturesSubpoolInput },
    { key: "cs2", label: "CS2", alloc: cs2AllocInput, setAlloc: setCs2AllocInput, fut: cs2FuturesSubpoolInput, setFut: setCs2FuturesSubpoolInput },
    { key: "lol", label: "LoL", alloc: lolAllocInput, setAlloc: setLolAllocInput, fut: lolFuturesSubpoolInput, setFut: setLolFuturesSubpoolInput },
  ];

  return (
    <PageShell title="Settings">
      {isError && (
        <div className="rounded-lg border border-[var(--color-critical)]/30 bg-[var(--color-critical)]/10 text-[var(--color-critical)] px-4 py-3 mb-6 text-sm">
          Could not reach the backend at http://127.0.0.1:8756 — is it running?
        </div>
      )}

      {isLoading || !data ? (
        <div className="text-sm text-[var(--color-text-dim)]">Loading…</div>
      ) : (
        <div className="max-w-3xl space-y-4">
          <AlertsSection />
          {/* 1 — Bankroll */}
          <SectionCard title="Bankroll">
            <div className="grid grid-cols-2 gap-3">
              <label className="text-xs text-[var(--color-text-dim)]">
                Total bankroll ($)
                <input type="number" min="0" step="1" value={bankrollInput} onChange={(e) => { setBankrollInput(e.target.value); idle(); }} className={`mt-1 ${NUM}`} />
              </label>
              <label className="text-xs text-[var(--color-text-dim)]">
                Bankroll size (units)
                <input type="number" min="1" step="1" value={unitsInput} onChange={(e) => { setUnitsInput(e.target.value); idle(); }} className={`mt-1 ${NUM}`} />
              </label>
            </div>
            <div className="flex flex-wrap gap-x-6 gap-y-1 mt-3 text-xs text-[var(--color-text-dim)]">
              <span>1 unit = <span className="text-[var(--color-text)] tabular-nums">${data.unit_dollars.toFixed(2)}</span></span>
              <span>Allocated across sports: <span className="text-[var(--color-text)] tabular-nums">{(data.total_allocation_pct * 100).toFixed(0)}%</span></span>
              <span className="text-[var(--color-text-muted)]">sports allocations are independent — they need not sum to 100%</span>
            </div>
          </SectionCard>

          {/* 2 — Staking rules */}
          <SectionCard
            title="Staking rules"
            sub="How big each suggested bet is. Deliberately conservative — every model here is unvalidated (model_validated: false), and full Kelly assumes a perfectly correct probability."
          >
            <div className="grid grid-cols-3 gap-3">
              <label className="text-xs text-[var(--color-text-dim)]">
                Kelly fraction (%)
                <input type="number" min="1" max="100" step="1" value={fractionalKellyInput} onChange={(e) => { setFractionalKellyInput(e.target.value); idle(); }} className={`mt-1 ${NUM}`} />
                <span className="block text-[10px] text-[var(--color-text-muted)] mt-1">fraction of full Kelly to stake — lower = safer</span>
              </label>
              <label className="text-xs text-[var(--color-text-dim)]">
                Max stake (% of pool)
                <input type="number" min="1" max="100" step="1" value={maxStakeInput} onChange={(e) => { setMaxStakeInput(e.target.value); idle(); }} className={`mt-1 ${NUM}`} />
                <span className="block text-[10px] text-[var(--color-text-muted)] mt-1">hard cap on any single position</span>
              </label>
              <label className="text-xs text-[var(--color-text-dim)]">
                Min. edge to bet (pp)
                <input type="number" min="0" max="100" step="1" value={minEdgeInput} onChange={(e) => { setMinEdgeInput(e.target.value); idle(); }} className={`mt-1 ${NUM}`} />
                <span className="block text-[10px] text-[var(--color-text-muted)] mt-1">min model-vs-market gap before staking</span>
              </label>
            </div>
          </SectionCard>

          {/* 3 — Per-sport allocation */}
          <SectionCard
            title="Per-sport allocation"
            sub="Each sport gets its own slice of the total bankroll, split between season-long futures (capital locked for months) and per-game markets (frees up as each game settles). Edit the two % columns; the resulting pools update on save."
          >
            <div className="overflow-x-auto">
              <table className="w-full text-sm border-collapse">
                <thead>
                  <tr className="text-[11px] uppercase tracking-wide text-[var(--color-text-dim)] border-b border-[var(--color-border)]">
                    <th className="text-left font-medium py-2 pr-3">Sport</th>
                    <th className="text-right font-medium py-2 px-2">Alloc %</th>
                    <th className="text-right font-medium py-2 px-2">Futures %</th>
                    <th className="text-right font-medium py-2 pl-3">Pool</th>
                    <th className="text-right font-medium py-2 pl-3">Per-game</th>
                    <th className="text-right font-medium py-2 pl-3">Futures</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--color-border)]">
                  {sportRows.map((s) => (
                    <tr key={s.key} className="hover:bg-[var(--color-surface)]">
                      <td className="py-2 pr-3 font-medium text-[var(--color-text)]">
                        {s.label}
                        <span className="block text-[10px] font-normal text-[var(--color-text-muted)]">{perGamePoolLabel(s.key).toLowerCase()}{s.noFutures ? " · moneyline-only" : ""}</span>
                      </td>
                      <td className="py-2 px-2 text-right">
                        <input type="number" min="1" max="100" step="1" value={s.alloc} onChange={(e) => { s.setAlloc(e.target.value); idle(); }} className={CELL_NUM} />
                      </td>
                      <td className="py-2 px-2 text-right">
                        {s.noFutures ? (
                          <span className="text-[var(--color-text-muted)] pr-2">—</span>
                        ) : (
                          <input type="number" min="0" max="100" step="1" value={s.fut} onChange={(e) => { s.setFut(e.target.value); idle(); }} className={CELL_NUM} />
                        )}
                      </td>
                      <td className="py-2 pl-3 text-right font-mono tabular-nums text-[var(--color-text)]">{poolVal(s.key, 0) !== null ? `$${poolVal(s.key, 0)!.toFixed(0)}` : "—"}</td>
                      <td className="py-2 pl-3 text-right font-mono tabular-nums text-[var(--color-text-dim)]">{poolVal(s.key, 1) !== null ? `$${poolVal(s.key, 1)!.toFixed(0)}` : "—"}<span className="text-[10px] text-[var(--color-text-muted)]"> · {unitStr(poolVal(s.key, 1) ?? 0)}</span></td>
                      <td className="py-2 pl-3 text-right font-mono tabular-nums text-[var(--color-text-dim)]">{poolVal(s.key, 2) ? `$${poolVal(s.key, 2)!.toFixed(0)}` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="text-[11px] text-[var(--color-text-muted)] mt-3 leading-relaxed">
              Not shown: WNBA has no futures sub-pool (moneyline-only); racing (F1/NASCAR/IndyCar) and college
              basketball/football are tracking-only (priced and logged for calibration, never staked), so they
              draw no bankroll slice. MMA and the esports titles default lighter on futures — their
              futures families (KXUFCTITLE, tournament winners) are thin/illiquid.
            </div>
          </SectionCard>

          {/* Save bar */}
          <div className="flex items-center gap-3">
            <button
              onClick={handleSave}
              disabled={saveState === "saving"}
              className="rounded-md bg-[var(--color-accent)] text-white px-5 py-2 text-sm font-medium hover:opacity-90 disabled:opacity-50 transition-opacity"
            >
              {saveState === "saving" ? "Saving…" : "Save changes"}
            </button>
            {saveState === "saved" && <span className="text-xs text-[var(--color-good)]">Saved.</span>}
            {saveState === "error" && (
              <span className="text-xs text-[var(--color-critical)]">Bankroll and units must be positive; all percentages between 0 and 100.</span>
            )}
          </div>
        </div>
      )}
    </PageShell>
  );
}
