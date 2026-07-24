import { apiGet } from "./client";

export interface HealthIssue {
  severity: "error" | "warning" | "info";
  category: string;
  sport: string;
  detail: string;
}
export interface HealthReport {
  checked_at: string;
  counts: { error: number; warning: number; info: number };
  issues: HealthIssue[];
}

export function fetchHealthCheck() {
  return apiGet<HealthReport>("/health-check");
}
