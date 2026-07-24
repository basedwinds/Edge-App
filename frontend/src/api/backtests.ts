import { apiGet, apiPost } from "./client";

export interface BacktestResult {
  key: string;
  sport: "nfl" | "nba" | "mlb" | "mma" | "tennis" | "soccer";
  label: string;
  summary: string;
  output: string;
  duration_sec: number;
  run_at: string;
}

export interface BacktestEntry {
  key: string;
  sport: "nfl" | "nba" | "mlb" | "mma" | "tennis" | "soccer";
  label: string;
  summary: string;
  result: BacktestResult | null;
}

export function fetchBacktests(): Promise<BacktestEntry[]> {
  return apiGet<BacktestEntry[]>("/backtests");
}

export function runAllBacktests(): Promise<BacktestResult[]> {
  return apiPost<BacktestResult[]>("/backtests/run");
}

export function runOneBacktest(key: string): Promise<BacktestResult> {
  return apiPost<BacktestResult>(`/backtests/run/${key}`);
}
