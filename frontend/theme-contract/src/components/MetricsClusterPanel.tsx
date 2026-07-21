import { CpuMetricCard } from "./CpuMetricCard";
import { GpuMetricCard } from "./GpuMetricCard";
import { MetricsShell } from "./MetricsShell";
import { PanelStage } from "./PanelStage";
import { RamMetricCard } from "./RamMetricCard";
import { StorageMetricCard } from "./StorageMetricCard";
import type { UniversalTaskSurfaceIdentity } from "../contracts/T023UniversalUiSystemSchemas";
import type { ProcessResourceAttribution } from "./MetricTaskProcessTruth";

export type MetricsClusterPanelProps = {
  gpu: number | null;
  cpu: number | null;
  ram: number | null;
  ramLabel?: string;
  read: number | null;
  write: number | null;
  readLabel?: string;
  writeLabel?: string;
  storageActivity?: number | null;
  demoActive?: boolean;
  className?: string;
  taskIdentity?: UniversalTaskSurfaceIdentity | null;
  resourceAttribution?: Partial<Record<"cpu" | "gpu" | "ram" | "storage", ProcessResourceAttribution>>;
};

export function MetricsClusterPanel({ gpu, cpu, ram, ramLabel, read, write, readLabel, writeLabel, storageActivity, className = "", taskIdentity = null, resourceAttribution = {} }: MetricsClusterPanelProps) {
  return (
    <MetricsShell className={`t023-panel t023-metrics-panel ${className}`.trim()} label="Evidence Lane attributable process metrics cluster">
      <PanelStage className="metrics-stage" label="Evidence Lane app process-tree metrics">
        <GpuMetricCard value={gpu} taskIdentity={taskIdentity} attribution={resourceAttribution.gpu} />
        <CpuMetricCard value={cpu} taskIdentity={taskIdentity} attribution={resourceAttribution.cpu} />
        <RamMetricCard value={ram} detailLabel={ramLabel} taskIdentity={taskIdentity} attribution={resourceAttribution.ram} />
        <StorageMetricCard read={read} write={write} readLabel={readLabel} writeLabel={writeLabel} activity={storageActivity} taskIdentity={taskIdentity} attribution={resourceAttribution.storage} />
      </PanelStage>
    </MetricsShell>
  );
}
