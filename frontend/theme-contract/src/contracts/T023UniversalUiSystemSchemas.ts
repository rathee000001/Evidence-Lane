export const t023UiSystemSchemas = Object.freeze({
  registry: "T023_UNIVERSAL_UI_SYSTEM_SCHEMA_V001",
  universalGlassPill: "T023_UNIVERSAL_GLASS_PILL_V001",
  universalGlassShell: "T023_UNIVERSAL_GLASS_SHELL_V001",
  universalFrostedPopup: "T023_UNIVERSAL_FROSTED_POPUP_V001",
  universalPopupFade: "T023_UNIVERSAL_POPUP_FADE_V001",
  universalGlassOrb: "T023_UNIVERSAL_GLASS_ORB_V001",
  universalBrainOrb: "T023_UNIVERSAL_BRAIN_ORB_V001",
  glassLayeringNoBleed: "T023_GLASS_LAYERING_NO_BLEED_V001",
  selectedBrainAuthority: "T023_CANONICAL_SELECTED_BRAIN_AUTHORITY_V1",
  universalProcessTaskEvent: "T023_UNIVERSAL_PROCESS_TASK_EVENT_V001",
});

export type UniversalProcessTaskStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "skipped"
  | "hil_waiting";

export type UniversalProcessTaskEvent = {
  schema: typeof t023UiSystemSchemas.universalProcessTaskEvent;
  task_id: string;
  run_id: string;
  request_id: string;
  project_id: string;
  brain_id: string;
  brain_name: string;
  workspace_dir: string;
  accepted_snapshot_id: string;
  candidate_delta_id: string;
  lane_id: string;
  stage_id: string;
  stage_name?: string;
  stage_order?: number;
  stage_count?: number;
  stage_percent?: number;
  tool_id: string;
  process_pid: number;
  process_tree_ids: number[];
  start_timestamp: string;
  update_timestamp: string;
  stop_timestamp: string;
  hil_timestamp: string;
  started_at?: string;
  updated_at?: string;
  status: UniversalProcessTaskStatus;
  completed_units: number;
  total_units: number;
  completed_count?: number;
  running_count?: number;
  queued_count?: number;
  cpu_percent: number | null;
  gpu_percent: number | null;
  ram_bytes: number | null;
  disk_read_bytes: number | null;
  disk_write_bytes: number | null;
  progress: number;
  global_percent?: number;
  elapsed_seconds?: number;
  eta_seconds?: number | null;
  failure_code: string;
  error_code?: string | null;
  skip_reason: string;
  hil_state: string;
  receipt_hash: string;
  next_pointer: string;
  command: string;
  active_command?: string;
  active_file?: string;
  pipeline_id?: string;
  loaded_lane_ids?: string[];
  skipped_lane_ids?: string[];
  metric_scope?: string;
  machine_wide_values_used?: boolean;
};

export type UniversalTaskSurfaceIdentity = {
  schema: typeof t023UiSystemSchemas.universalProcessTaskEvent;
  taskId: string;
  runId: string;
  requestId: string;
  brainId: string;
  brainName: string;
  laneId: string;
  stageId: string;
  stageName: string;
  toolId: string;
  processPid: number;
  processTreeIds: number[];
  status: UniversalProcessTaskStatus;
};

export function universalTaskSurfaceIdentity(
  event: Partial<UniversalProcessTaskEvent> | null | undefined,
): UniversalTaskSurfaceIdentity | null {
  if (
    event?.schema !== t023UiSystemSchemas.universalProcessTaskEvent
    || !String(event.task_id || "").trim()
    || !String(event.run_id || "").trim()
  ) return null;
  return {
    schema: t023UiSystemSchemas.universalProcessTaskEvent,
    taskId: String(event.task_id),
    runId: String(event.run_id),
    requestId: String(event.request_id || ""),
    brainId: String(event.brain_id || ""),
    brainName: String(event.brain_name || ""),
    laneId: String(event.lane_id || ""),
    stageId: String(event.stage_id || ""),
    stageName: String(event.stage_name || event.active_command || event.stage_id || "Task running"),
    toolId: String(event.tool_id || ""),
    processPid: Math.max(0, Number(event.process_pid) || 0),
    processTreeIds: Array.isArray(event.process_tree_ids)
      ? event.process_tree_ids.map(Number).filter((pid) => Number.isInteger(pid) && pid > 0)
      : [],
    status: (event.status || "queued") as UniversalProcessTaskStatus,
  };
}

export type SelectedBrainAuthorityMode =
  | "UNBOUND"
  | "PENDING"
  | "COMMITTED"
  | "BROWSE_ONLY_BACKEND_UNAVAILABLE";
