import type { UniversalTaskSurfaceIdentity } from "../contracts/T023UniversalUiSystemSchemas";

export type ProcessResourceAttribution = {
  process_pid?: number;
  process_tree_ids?: number[];
  task?: string;
  stage_id?: string;
  pipeline_id?: string;
  attribution_status?: string;
};

export type MetricTaskProcessTruthProps = {
  metric: "cpu" | "gpu" | "ram" | "storage";
  taskIdentity?: UniversalTaskSurfaceIdentity | null;
  attribution?: ProcessResourceAttribution | null;
};

export function MetricTaskProcessTruth({ metric, taskIdentity = null, attribution = null }: MetricTaskProcessTruthProps) {
  const authoritative = Boolean(taskIdentity?.taskId && taskIdentity?.runId);
  const active = authoritative && taskIdentity?.status === "running";
  const attributedPid = Math.max(0, Number(attribution?.process_pid || 0));
  const stageLabel = active ? (attribution?.stage_id || taskIdentity?.stageName || taskIdentity?.stageId || "Task") : "Idle";
  const taskLabel = active ? (attribution?.task && attribution.task !== "Idle" ? attribution.task : taskIdentity?.toolId || "Task") : "Idle";
  const processLabel = active && (attributedPid || taskIdentity?.processPid) ? `PID ${attributedPid || taskIdentity?.processPid}` : "Idle";
  return <div
    className={`metric-task-process-truth ${active ? "is-active" : "is-inactive"}`}
    data-metric-task-resource={metric}
    data-task-event-schema={taskIdentity?.schema || "IDLE"}
    data-task-id={taskIdentity?.taskId || ""}
    data-run-id={taskIdentity?.runId || ""}
    data-stage-id={taskIdentity?.stageId || ""}
    data-tool-id={taskIdentity?.toolId || ""}
    data-process-pid={attributedPid || taskIdentity?.processPid || 0}
    data-process-tree-ids={attribution?.process_tree_ids?.join(",") || taskIdentity?.processTreeIds.join(",") || ""}
    data-resource-attribution-status={attribution?.attribution_status || (active ? "TASK_IDENTITY_PENDING_SAMPLE" : "IDLE")}
    data-pid-authority="LIVE_SAMPLED_RESOURCE_PROCESS"
    data-task-authority="LIVE_SAMPLED_ACTIVE_STAGE"
    data-task-status={taskIdentity?.status || "idle"}
    title={`${taskLabel} · ${stageLabel} · ${processLabel}`}
  >
    <span>{active ? `${taskLabel} · ${stageLabel}` : "Idle"}</span>
    <code>{processLabel}</code>
  </div>;
}
