export type MetricState = "normal" | "elevated" | "high" | "critical" | "unavailable";

export const METRIC_ELEVATED_THRESHOLD = 70;
export const METRIC_HIGH_THRESHOLD = 85;
export const METRIC_CRITICAL_THRESHOLD = 95;

export function normalizeMetricValue(value: number | null) {
  const available = typeof value === "number" && Number.isFinite(value);
  const safe = available ? Math.max(0, Math.min(100, value)) : 0;
  return { available, safe };
}

export function isMetricCritical(value: number | null) {
  const { available, safe } = normalizeMetricValue(value);
  return available && safe >= METRIC_CRITICAL_THRESHOLD;
}

export function metricState(value: number | null): MetricState {
  const { available, safe } = normalizeMetricValue(value);
  if (!available) return "unavailable";
  if (safe >= METRIC_CRITICAL_THRESHOLD) return "critical";
  if (safe >= METRIC_HIGH_THRESHOLD) return "high";
  if (safe >= METRIC_ELEVATED_THRESHOLD) return "elevated";
  return "normal";
}

export function metricStateClass(state: MetricState) {
  return state === "unavailable" ? "is-unavailable" : `is-${state}`;
}

export function metricStateLabel(state: MetricState) {
  return state[0].toUpperCase() + state.slice(1);
}
