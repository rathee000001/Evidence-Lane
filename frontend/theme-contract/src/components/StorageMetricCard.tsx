import ssdIcon from "../assets/metrics/ssd-icon.png";
import { GlassShell } from "./GlassShell";
import { MetricIdentityOrb } from "./MetricIdentityOrb";
import { metricState, metricStateClass, metricStateLabel, normalizeMetricValue } from "./metricState";
import type { UniversalTaskSurfaceIdentity } from "../contracts/T023UniversalUiSystemSchemas";
import { MetricTaskProcessTruth, type ProcessResourceAttribution } from "./MetricTaskProcessTruth";

export type StorageMetricCardProps = {
  read: number | null;
  write: number | null;
  readLabel?: string;
  writeLabel?: string;
  activity?: number | null;
  taskIdentity?: UniversalTaskSurfaceIdentity | null;
  attribution?: ProcessResourceAttribution | null;
};

export function StorageMetricCard({ read, write, readLabel, writeLabel, activity, taskIdentity = null, attribution = null }: StorageMetricCardProps) {
  const { available: readAvailable, safe: r } = normalizeMetricValue(read);
  const { available: writeAvailable, safe: w } = normalizeMetricValue(write);
  const { available: activityAvailable, safe: diskActivity } = normalizeMetricValue(activity ?? null);
  const state = metricState(activity ?? null);
  const available = activityAvailable && readAvailable && writeAvailable;
  return (
    <GlassShell className={`metric-card metric-card--storage ${metricStateClass(state)} ${state === "critical" ? "is-danger" : ""}`} label="Disk process metric" data-metric-state={state}>
      <header className="metric-card__header"><span className="metric-card__identity"><MetricIdentityOrb src={ssdIcon} label="Storage" tone="storage" /><b>SSD/HDD</b></span><span aria-hidden="true">•••</span></header>
      <strong className="storage-meter__activity">App I/O peak-relative · {activityAvailable ? `${Math.round(diskActivity)}%` : "Idle"}</strong>
      <div className="storage-meter"><span>Read · {readLabel ?? (readAvailable ? `${Math.round(r)}%` : "Idle")}</span><div className="storage-meter__track"><i style={{ width: `${r}%` }} /></div></div>
      <div className="storage-meter"><span>Write · {writeLabel ?? (writeAvailable ? `${Math.round(w)}%` : "Idle")}</span><div className="storage-meter__track"><i style={{ width: `${w}%` }} /></div></div>
      <MetricTaskProcessTruth metric="storage" taskIdentity={taskIdentity} attribution={attribution} />
      <footer className="metric-card__footer">Disk I/O · {available ? metricStateLabel(state) : "Idle"}</footer>
    </GlassShell>
  );
}
