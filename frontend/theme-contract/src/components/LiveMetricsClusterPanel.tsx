import { useEffect, useState } from "react";
import {
  universalTaskSurfaceIdentity,
  type UniversalProcessTaskEvent,
} from "../contracts/T023UniversalUiSystemSchemas";
import { MetricsClusterPanel } from "./MetricsClusterPanel";
import type { ProcessResourceAttribution } from "./MetricTaskProcessTruth";

type ProcessTelemetry = {
  metric_scope?: string;
  machine_wide_values_used?: boolean;
  metrics_status?: string;
  cpu_percent?: number | null;
  gpu_attribution_status?: string;
  gpu_percent?: number | null;
  working_set_bytes?: number | null;
  working_set_percent_of_system?: number | null;
  disk_read_bytes_per_second?: number | null;
  disk_write_bytes_per_second?: number | null;
  disk_total_bytes_per_second?: number | null;
  peak_disk_total_bytes_per_second?: number | null;
  sampled_process_count?: number | null;
  active_process_pid?: number | null;
  resource_attribution?: Partial<Record<"cpu" | "gpu" | "ram" | "storage", ProcessResourceAttribution>>;
};

type TelemetryEnvelope = {
  ok: boolean;
  result?: ProcessTelemetry;
  error?: string;
};

type LiveMetricsClusterPanelProps = {
  brainName: string;
  workspaceDir?: string;
  className?: string;
  taskEvent?: Partial<UniversalProcessTaskEvent> | null;
};

const POLL_DELAY_MS = 1_800;

function finiteMetric(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function ioLabel(value: number | null): string {
  if (value === null) return "Idle";
  const megabytes = value / 1_000_000;
  return `${megabytes.toFixed(megabytes >= 10 ? 1 : 2)} MB/s`;
}

export function LiveMetricsClusterPanel({ brainName, workspaceDir = "", className = "", taskEvent = null }: LiveMetricsClusterPanelProps) {
  const [telemetry, setTelemetry] = useState<ProcessTelemetry>({});
  const taskIdentity = universalTaskSurfaceIdentity(taskEvent);
  const activeTask = taskIdentity?.status === "running" && taskIdentity.processPid > 0;
  const [state, setState] = useState<"idle" | "connecting" | "live" | "unavailable">(activeTask ? "connecting" : "idle");

  useEffect(() => {
    if (!activeTask) {
      setTelemetry({});
      setState("idle");
      return undefined;
    }
    let mounted = true;
    let timer = 0;
    setTelemetry({});
    setState("connecting");

    const sample = async () => {
      try {
        const nativeTransport = window.__EVIDENCE_OS_NATIVE_COMMAND_TRANSPORT__;
        if (!nativeTransport) {
          throw new Error("NATIVE_BACKEND_TRANSPORT_UNAVAILABLE");
        }
        const payload = await nativeTransport("process.metrics.snapshot", {
          brain_name: brainName,
          workspace_dir: workspaceDir,
          task_id: taskIdentity.taskId,
          run_id: taskIdentity.runId,
          process_pid: taskIdentity.processPid,
        }) as TelemetryEnvelope;
        const snapshot = payload.result;
        if (!payload.ok || !snapshot) {
          throw new Error(payload.error || "TELEMETRY_PAYLOAD_UNAVAILABLE");
        }
        if (snapshot.metric_scope !== "EVIDENCE_LANE_APP_PROCESS_TREE" || snapshot.machine_wide_values_used !== false) {
          throw new Error("PROCESS_TREE_ATTRIBUTION_REQUIRED");
        }
        if (!mounted) return;
        setTelemetry(snapshot);
        setState("live");
      } catch (error) {
        if (!mounted) return;
        setState("unavailable");
      } finally {
        if (mounted) timer = window.setTimeout(sample, POLL_DELAY_MS);
      }
    };

    void sample();
    return () => {
      mounted = false;
      window.clearTimeout(timer);
    };
  }, [activeTask, brainName, taskIdentity?.processPid, taskIdentity?.runId, taskIdentity?.taskId, workspaceDir]);

  const cpu = activeTask ? finiteMetric(telemetry.cpu_percent) : null;
  const gpu = activeTask && telemetry.gpu_attribution_status === "AVAILABLE_PROCESS_ATTRIBUTION"
    ? finiteMetric(telemetry.gpu_percent)
    : null;
  const ram = activeTask ? finiteMetric(telemetry.working_set_percent_of_system) : null;
  const workingSet = activeTask ? finiteMetric(telemetry.working_set_bytes) : null;
  const read = activeTask ? finiteMetric(telemetry.disk_read_bytes_per_second) : null;
  const write = activeTask ? finiteMetric(telemetry.disk_write_bytes_per_second) : null;
  const ioTotal = activeTask ? finiteMetric(telemetry.disk_total_bytes_per_second) : null;
  const ioPeak = activeTask ? finiteMetric(telemetry.peak_disk_total_bytes_per_second) : null;
  const activity = ioTotal === null || ioPeak === null
    ? null
    : ioPeak <= 0 ? (ioTotal > 0 ? 100 : 0) : Math.max(0, Math.min(100, (ioTotal / ioPeak) * 100));
  const readVisual = read === null || ioPeak === null || ioPeak <= 0 ? null : Math.min(100, (read / ioPeak) * 100);
  const writeVisual = write === null || ioPeak === null || ioPeak <= 0 ? null : Math.min(100, (write / ioPeak) * 100);
  const ramLabel = workingSet === null ? "Idle" : `${(workingSet / 1_048_576).toFixed(1)} MiB`;

  return (
    <div
      className="live-metrics-adapter"
      data-telemetry-state={state}
      data-metric-scope={activeTask ? telemetry.metric_scope || "IDLE" : "IDLE_NO_ACTIVE_TASK"}
      data-process-count={activeTask ? finiteMetric(telemetry.sampled_process_count) ?? 0 : 0}
      data-task-event-schema={taskIdentity?.schema || "UNAVAILABLE"}
      data-task-id={taskIdentity?.taskId || ""}
      data-run-id={taskIdentity?.runId || ""}
      data-request-id={taskIdentity?.requestId || ""}
      data-brain-id={taskIdentity?.brainId || ""}
      data-lane-id={taskIdentity?.laneId || ""}
      data-stage-id={taskIdentity?.stageId || ""}
      data-tool-id={taskIdentity?.toolId || ""}
      data-process-pid={activeTask ? finiteMetric(telemetry.active_process_pid) ?? taskIdentity?.processPid ?? 0 : 0}
      data-process-tree-ids={taskIdentity?.processTreeIds.join(",") || ""}
      data-task-status={taskIdentity?.status || "idle"}
      data-receipt-hash={String(taskEvent?.receipt_hash || "")}
      data-task-event-updated-at={String(taskEvent?.update_timestamp || taskEvent?.updated_at || "")}
    >
      <MetricsClusterPanel
        className={className}
        gpu={gpu}
        cpu={cpu}
        ram={ram}
        ramLabel={ramLabel}
        read={readVisual}
        write={writeVisual}
        readLabel={ioLabel(read)}
        writeLabel={ioLabel(write)}
        storageActivity={activity}
        taskIdentity={taskIdentity}
        resourceAttribution={telemetry.resource_attribution || {}}
      />
    </div>
  );
}
