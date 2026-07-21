import gpuIcon from "../assets/metrics/gpu-icon.png";
import { GlassShell } from "./GlassShell";
import { MetricIdentityOrb } from "./MetricIdentityOrb";
import { MetricGauge } from "./MetricGauge";
import { metricState, metricStateClass, metricStateLabel, normalizeMetricValue } from "./metricState";
import type { UniversalTaskSurfaceIdentity } from "../contracts/T023UniversalUiSystemSchemas";
import { MetricTaskProcessTruth, type ProcessResourceAttribution } from "./MetricTaskProcessTruth";

export function GpuMetricCard({ value, taskIdentity = null, attribution = null }: { value: number | null; taskIdentity?: UniversalTaskSurfaceIdentity | null; attribution?: ProcessResourceAttribution | null }) {
  const { available } = normalizeMetricValue(value);
  const state = metricState(value);
  return <GlassShell className={`metric-card ${metricStateClass(state)} ${state === "critical" ? "is-danger" : ""}`} label="GPU process metric" data-metric-state={state}><header className="metric-card__header"><span className="metric-card__identity"><MetricIdentityOrb src={gpuIcon} label="GPU" tone="gpu" /><b>GPU</b></span><span aria-hidden="true">•••</span></header><MetricGauge value={value} label="GPU" /><MetricTaskProcessTruth metric="gpu" taskIdentity={taskIdentity} attribution={attribution} /><footer className="metric-card__footer">GPU process · {available ? metricStateLabel(state) : "Idle"}</footer></GlassShell>;
}
