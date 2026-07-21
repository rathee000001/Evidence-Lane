import ramIcon from "../assets/metrics/ram-icon.svg";
import { GlassShell } from "./GlassShell";
import { MetricIdentityOrb } from "./MetricIdentityOrb";
import { metricState, metricStateClass, metricStateLabel, normalizeMetricValue } from "./metricState";
import type { UniversalTaskSurfaceIdentity } from "../contracts/T023UniversalUiSystemSchemas";
import { MetricTaskProcessTruth, type ProcessResourceAttribution } from "./MetricTaskProcessTruth";

export function RamMetricCard({ value, detailLabel, taskIdentity = null, attribution = null }: { value: number | null; detailLabel?: string; taskIdentity?: UniversalTaskSurfaceIdentity | null; attribution?: ProcessResourceAttribution | null }) {
  const { available, safe } = normalizeMetricValue(value);
  const state = metricState(value);
  const filled = Math.round(safe / 5);
  return (
    <GlassShell className={`metric-card metric-card--bar ${metricStateClass(state)} ${state === "critical" ? "is-danger" : ""}`} label="RAM process metric" data-metric-state={state}>
      <header className="metric-card__header"><span className="metric-card__identity"><MetricIdentityOrb src={ramIcon} label="RAM" tone="ram" /><b>RAM</b></span><span aria-hidden="true">•••</span></header>
      <div className="segmented-meter" aria-label={`RAM ${available ? `${safe}%` : "idle"}`}>
        <div className="segmented-meter__value">{available ? `${Math.round(safe)}%` : "Idle"}</div>
        <div className="segmented-meter__track">{Array.from({ length: 20 }, (_, index) => <i className={index < filled ? "is-filled" : ""} key={index} />)}</div>
      </div>
      <MetricTaskProcessTruth metric="ram" taskIdentity={taskIdentity} attribution={attribution} />
      <footer className="metric-card__footer">RAM · {available ? `${detailLabel || "measured"} · ${metricStateLabel(state)}` : "Idle"}</footer>
    </GlassShell>
  );
}
