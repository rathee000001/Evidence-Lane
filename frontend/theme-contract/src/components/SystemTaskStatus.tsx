import type { ReactNode } from "react";
import {
  universalTaskSurfaceIdentity,
  type UniversalProcessTaskEvent,
  type UniversalTaskSurfaceIdentity,
} from "../contracts/T023UniversalUiSystemSchemas";
import { ExpandableDetailSlot, FieldStack, MetricField, StatusField } from "./FieldPrimitives";
import { GlassIconOrb } from "./GlassIconOrb";
import { GlassPill } from "./GlassPill";
import { PanelStage } from "./PanelStage";
import { TaskStatusShell } from "./TaskStatusShell";

export type TaskQueueItem = { id: string; label: string; state: "running" | "queued" | "complete" | "failed" };
export type TaskAction = "rollback" | "pause" | "view" | "flash" | "output";

export type SystemTaskStatusProps = {
  progress: number;
  task: string;
  detail?: string;
  running: number;
  queued: number;
  completed: number;
  elapsed: string;
  stage?: string;
  queue?: TaskQueueItem[];
  actions?: ReactNode;
  status?: "waiting" | "running" | "completed" | "failed";
  className?: string;
  onAction?: (action: TaskAction) => void;
  taskIdentity?: UniversalTaskSurfaceIdentity | null;
  taskEvent?: Partial<UniversalProcessTaskEvent> | null;
};

function ActionIcon({ type }: { type: TaskAction }) {
  const content = {
    rollback: <><path d="M5 8H2V5" /><path d="M3 7a8 8 0 1 1 1 7" /><path d="M10 7v5l3 2" /></>,
    pause: <><path d="M7 5v14M17 5v14" /></>,
    view: <><path d="M2 12s4-6 10-6 10 6 10 6-4 6-10 6S2 12 2 12Z" /><circle cx="12" cy="12" r="2.5" /></>,
    flash: <><path d="m13 2-8 12h7l-1 8 8-12h-7l1-8Z" /></>,
    output: <><path d="M4 5h7l2 2h7v12H4Z" /><path d="M12 10v6M9 13l3 3 3-3" /></>,
  }[type];
  return <svg className="task-action-icon" viewBox="0 0 24 24" aria-hidden="true">{content}</svg>;
}

export function SystemTaskStatus({
  progress,
  task,
  detail,
  running,
  queued,
  completed,
  elapsed,
  stage = "Preview",
  queue = [],
  actions,
  status = "running",
  className = "",
  onAction,
  taskIdentity = null,
  taskEvent = null,
}: SystemTaskStatusProps) {
  const surfaceIdentity = universalTaskSurfaceIdentity(taskEvent) || taskIdentity;
  const safe = Math.max(0, Math.min(100, Math.round(progress)));
  const danger = status === "failed";
  const success = status === "completed" || (safe === 100 && !danger);
  const filled = Math.round(safe / 4);
  const action = (name: TaskAction) => onAction?.(name);
  const actionDisabled = !onAction;
  const tone = danger ? "red" : success ? "green" : status === "waiting" ? "gold" : "cyan";

  return (
    <TaskStatusShell className={`t023-panel task-process-card ${danger ? "is-danger" : ""} ${success ? "is-complete" : ""} ${className}`.trim()} label="System task status">
      <PanelStage className="task-process-stage" label="Centered System Task stage">
        <header className="task-process__header">
          <div><small>EVIDENCE LANE PROCESS · REAL BACKEND</small><h1>System Task Status</h1></div>
          <strong>{safe}%</strong>
        </header>
        <div className="task-process__meter" aria-label={`Task progress ${safe}%`}>
          {Array.from({ length: 25 }, (_, index) => <i className={index < filled ? "is-filled" : ""} key={index} />)}
        </div>
        <div
          className="task-process__body"
          data-task-event-schema={surfaceIdentity?.schema || "UNAVAILABLE"}
          data-task-id={surfaceIdentity?.taskId || ""}
          data-run-id={surfaceIdentity?.runId || ""}
          data-request-id={surfaceIdentity?.requestId || ""}
          data-brain-id={surfaceIdentity?.brainId || ""}
          data-lane-id={surfaceIdentity?.laneId || ""}
          data-stage-id={surfaceIdentity?.stageId || ""}
          data-tool-id={surfaceIdentity?.toolId || ""}
          data-process-pid={surfaceIdentity?.processPid || 0}
          data-process-tree-ids={surfaceIdentity?.processTreeIds.join(",") || ""}
          data-task-status={surfaceIdentity?.status || "unavailable"}
          data-receipt-hash={String(taskEvent?.receipt_hash || "")}
          data-task-event-updated-at={String(taskEvent?.update_timestamp || taskEvent?.updated_at || "")}
        >
          <StatusField label="ACTIVE TASK" title={task} detail={detail} tone={tone} />
          <FieldStack className="task-process__stats">
            <MetricField label="Running" value={running} />
            <MetricField label="Queued" value={queued} />
            <MetricField label="Completed" value={completed} />
            <MetricField label="Elapsed" value={elapsed} />
            <MetricField label="Stage" value={stage} />
          </FieldStack>
        </div>
        <footer className="task-process__footer">
          <span className="task-process__demo-label">BACKEND STATE</span>
          <div className="task-process__controls">
            <GlassPill className="task-process__rollback" variant={actionDisabled ? "disabled" : "default"} disabled={actionDisabled} leading={<GlassIconOrb className="task-action-orb" color="#69d9f5" size={26} decorative><ActionIcon type="rollback" /></GlassIconOrb>} onClick={() => action("rollback")}>
              <span className="task-process__rollback-full">Roll Back Version Control</span><span className="task-process__rollback-short">Rollback</span>
            </GlassPill>
            <div className="task-process__actions">
              {actions ?? <>
                <span className="task-process__secondary-actions">
                  <GlassPill iconOnly aria-label="Pause" variant={actionDisabled ? "disabled" : "default"} disabled={actionDisabled} leading={<GlassIconOrb className="task-action-orb" color="#69d9f5" size={26} decorative><ActionIcon type="pause" /></GlassIconOrb>} onClick={() => action("pause")} />
                  <GlassPill iconOnly aria-label="View" variant={actionDisabled ? "disabled" : "default"} disabled={actionDisabled} leading={<GlassIconOrb className="task-action-orb" color="#69d9f5" size={26} decorative><ActionIcon type="view" /></GlassIconOrb>} onClick={() => action("view")} />
                  <GlassPill iconOnly aria-label="Flash" variant={actionDisabled ? "disabled" : "default"} disabled={actionDisabled} leading={<GlassIconOrb className="task-action-orb" color="#efca72" size={26} decorative><ActionIcon type="flash" /></GlassIconOrb>} onClick={() => action("flash")} />
                  <GlassPill iconOnly aria-label="Output" variant={actionDisabled ? "disabled" : "default"} disabled={actionDisabled} leading={<GlassIconOrb className="task-action-orb" color="#69d9f5" size={26} decorative><ActionIcon type="output" /></GlassIconOrb>} onClick={() => action("output")} />
                </span>
                <ExpandableDetailSlot label="Details">
                  <div className="task-process__detail-list">
                    {queue.length ? queue.map((item) => <span className={`task-process__queue-item is-${item.state}`} key={item.id}><i />{item.label}</span>) : <span>No persisted backend task list is available for this brain yet.</span>}
                  </div>
                </ExpandableDetailSlot>
              </>}
            </div>
          </div>
        </footer>
      </PanelStage>
    </TaskStatusShell>
  );
}
