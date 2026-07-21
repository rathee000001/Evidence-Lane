import type { EvidenceUiCommand, SimulationReceipt } from "./EvidenceOSSimulationContract";

export type TelemetryTone = "NEUTRAL" | "GREEN" | "RED";

export type TelemetryNodeKind =
  | "brain"
  | "source"
  | "folder"
  | "file"
  | "symbol"
  | "route"
  | "test"
  | "dependency"
  | "artifact"
  | "package"
  | "validation"
  | "change"
  | string;

export type TelemetryNode = {
  node_id: string;
  kind: TelemetryNodeKind;
  label: string;
  evidence_state: string;
  tone: TelemetryTone;
  evidence_refs: string[];
  overlay_evidence_direct?: boolean;
  source_id?: string;
  lane_id?: string;
  relative_path?: string;
  open_target?: string;
  language?: string;
  size_bytes?: number;
  [key: string]: unknown;
};

export type TelemetryRelationEdge = {
  edge_id: string;
  from_node_id: string;
  to_node_id: string;
  relation_type: string;
  evidence_state: string;
  tone: TelemetryTone;
  evidence_refs: string[];
  overlay_evidence_direct?: boolean;
  derived_from_indexed_path?: boolean;
};

export type TelemetryDelta = {
  delta_id: string;
  display_name: string;
  ordinal: number;
  before_version_id: string;
  after_version_id: string;
  before_snapshot_hash: string;
  after_snapshot_hash: string;
  diff_authority: string;
  changed_object_count: number;
  test_status: string;
  build_status: string;
  diff_status: string;
  evidence_state: string;
  tone: TelemetryTone;
};

export type TelemetryContractResult = {
  schema: string;
  read_only: boolean;
  creates_sqlite_schema: boolean;
  duplicates_topology: boolean;
  raw_project_reread_for_graph: boolean;
  automatic_fusion: boolean;
  production_3d_ui_status: string;
  commands: string[];
};

export type TelemetrySnapshotResult = {
  schema: string;
  status: string;
  brain_name: string;
  brain_root: string;
  current_version?: {
    version_id?: string;
    snapshot_hash?: string;
    integrity_status?: string;
    current_binding_status?: string;
  };
  delta_count: number;
  read_only: boolean;
};

export type TelemetryDeltaListResult = {
  schema: string;
  status: string;
  brain_name: string;
  version_count: number;
  delta_count: number;
  deltas: TelemetryDelta[];
  truncated: boolean;
  max_deltas: number;
  read_only: boolean;
};

export type TelemetryGraphResult = {
  schema: string;
  status: string;
  brain_name: string;
  snapshot: {
    version_id?: string;
    snapshot_hash?: string;
    integrity_status?: string;
  };
  selected_delta: TelemetryDelta | null;
  scope: {
    scope_id: string | null;
    mode: "FULL_INDEXED_GRAPH" | "REPLACED_WITH_INDEXED_SUBGRAPH";
  };
  nodes: TelemetryNode[];
  edges: TelemetryRelationEdge[];
  counts: {
    sector_databases: number;
    nodes_returned: number;
    nodes_total_in_scope: number;
    edges_returned: number;
  };
  truncated: boolean;
  max_nodes: number;
  read_only: boolean;
  raw_project_files_reread: number;
  duplicate_topology_created: boolean;
  automatic_fusion: boolean;
};

/**
 * The one-file, build/Fuse-materialized topology payload returned as part of
 * brain.select. Ordinary brain switching consumes this object synchronously;
 * it never fans out into telemetry commands or scans source/index state.
 */
export type PersistedTelemetryBundle = {
  schema: "EVIDENCEOS_ATOMIC_ACCEPTED_TELEMETRY_BUNDLE_V1";
  status: "READY" | "NOT_MATERIALIZED";
  brain_name: string;
  accepted_version_id?: string;
  accepted_snapshot_hash?: string;
  immutable_index_key?: string;
  bundle_sha256?: string;
  cache_path?: string;
  contract?: TelemetryContractResult;
  snapshot?: TelemetrySnapshotResult;
  deltas?: TelemetryDeltaListResult;
  graph?: TelemetryGraphResult;
  error_code?: string;
  hydration_law: string;
  raw_project_files_reread: 0;
  index_probe_count: 0;
  build_count: 0;
  refresh_count: 0;
  fuse_count: 0;
  mutation_count: 0;
};

export type TelemetryExecute = (
  command: EvidenceUiCommand,
  label: string,
  payload?: Record<string, unknown>,
) => Promise<SimulationReceipt>;

export function telemetryResult<T>(receipt: SimulationReceipt, command: string): T {
  if (!receipt.ok) {
    throw new Error(receipt.error || `${command.toUpperCase().replaceAll(".", "_")}_FAILED`);
  }
  if (!receipt.result || typeof receipt.result !== "object" || Array.isArray(receipt.result)) {
    throw new Error(`${command.toUpperCase().replaceAll(".", "_")}_INVALID_RESULT`);
  }
  return receipt.result as T;
}

export function telemetryNodePath(node: TelemetryNode): string {
  return String(node.open_target || node.relative_path || node.label || "").trim();
}

export function telemetryErrorCode(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error || "TELEMETRY_UNAVAILABLE");
  const match = raw.match(/[A-Z][A-Z0-9_]{4,}/g);
  return match?.at(-1) || raw;
}

export const TELEMETRY_BACKEND_COMMANDS = Object.freeze([
  "brain.telemetry.contract",
  "brain.telemetry.snapshot",
  "brain.telemetry.deltas",
  "brain.telemetry.graph",
  "brain.telemetry.openTarget",
] as const);
