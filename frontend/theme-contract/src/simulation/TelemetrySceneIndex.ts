export type TelemetryScenePoint3 = [number, number, number];

export type TelemetrySceneShellClass =
  | "FIXED_OUTER_FOLDER_NODE_ORB"
  | "FILE_EDGE_NODE_ORB"
  | "TERMINAL_ROOT_FOLDER_BUTTON_ORB";

export type TelemetrySceneShellIndex = {
  brain_id: string;
  topology_snapshot_hash: string;
  active_root_or_folder_id: string;
  scene_level_index: number;
  shell_id: string;
  shell_class: TelemetrySceneShellClass;
  parent_shell_id: string | null;
  child_shell_id: string | null;
  node_ids: string[];
  node_count: number;
  capacity: number | null;
  canonical_order: string[];
  canonical_partition_index: number | null;
  radius_ratio: number;
  depth_position: number;
  active: boolean;
  saved_orientation: TelemetryScenePoint3;
  selected_brain_color_reference: string;
  node_positions: Record<string, TelemetryScenePoint3>;
  glass_volume_transparent: true;
  selected_color_application: "OUTER_RIM_ONLY";
  connector_line_count: 0;
  active_presentation_law: "CAMERA_DOLLY_COMMON_ENVELOPE_NO_MESH_SCALE_POP";
  back_layer_presence_law: "OUTER_SHELLS_RETAINED_BEHIND_ACTIVE";
  reflection_motion_law: "SHELL_LOCAL_HIGHLIGHTS_ROTATE_WITH_ORB";
};

export type TelemetrySceneIndex = {
  schema: "T023_DERIVED_SAVED_TOPOLOGY_SCENE_INDEX_V1";
  cache_key: string;
  brain_id: string;
  topology_snapshot_hash: string;
  active_root_or_folder_id: string;
  file_edge_capacity: 20;
  file_edge_count: number;
  child_folder_count: number;
  file_edge_partition_distribution: number[];
  canonical_file_edge_node_ids: string[];
  canonical_child_folder_node_ids: string[];
  ordered_shell_ids: string[];
  total_glass_orb_count: number;
  scene_transition_count: number;
  terminal_root_shell_id: string;
  fixed_outer_folder_shell_id: string;
  selected_brain_color_reference: string;
  shells: TelemetrySceneShellIndex[];
  raw_project_files_reread: 0;
  canonical_project_truth_mutated: false;
};

export type TelemetrySceneIndexInput = {
  brainId: string;
  topologySnapshotHash: string;
  activeRootOrFolderId: string;
  childFolderNodeIds: string[];
  fileEdgeNodeIds: string[];
  selectedBrainColorReference: string;
};

export type TelemetrySceneIndexResolution = {
  index: TelemetrySceneIndex;
  cacheStatus: "HIT" | "MATERIALIZED";
};

export const TELEMETRY_FILE_EDGE_CAPACITY = 20 as const;

export function resolveTelemetrySceneStep(
  orderedShellIds: string[],
  focusedShellId: string | null,
  initialHoveredShellId: string | null,
  direction: "INWARD" | "OUTWARD",
) {
  if (!orderedShellIds.length) return null;
  const currentId = focusedShellId && orderedShellIds.includes(focusedShellId)
    ? focusedShellId
    : initialHoveredShellId && orderedShellIds.includes(initialHoveredShellId)
      ? initialHoveredShellId
      : orderedShellIds[0];
  const currentIndex = Math.max(0, orderedShellIds.indexOf(currentId));
  const nextIndex = direction === "INWARD"
    ? Math.min(orderedShellIds.length - 1, currentIndex + 1)
    : Math.max(0, currentIndex - 1);
  return orderedShellIds[nextIndex];
}

const sceneIndexCache = new Map<string, TelemetrySceneIndex>();

function stableHash32(value: string) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function stableHex(value: string) {
  return stableHash32(value).toString(16).padStart(8, "0");
}

function stableUnit(value: string) {
  return stableHash32(value) / 0xffffffff;
}

function canonicalSpherePoint(index: number, total: number, phase: number): TelemetryScenePoint3 {
  const count = Math.max(1, total);
  const y = 1 - ((index + 0.5) * 2) / count;
  const planarRadius = Math.sqrt(Math.max(0, 1 - y * y));
  const theta = index * Math.PI * (3 - Math.sqrt(5)) + phase;
  return [Math.cos(theta) * planarRadius, y, Math.sin(theta) * planarRadius];
}

function assertUnique(label: string, ids: string[]) {
  if (new Set(ids).size !== ids.length) throw new Error(`TELEMETRY_SCENE_INDEX_${label}_DUPLICATE_NODE_ID`);
}

export function partitionTelemetryFileEdgeNodeIds(
  canonicalNodeIds: string[],
  capacity = TELEMETRY_FILE_EDGE_CAPACITY,
) {
  if (!Number.isInteger(capacity) || capacity <= 0) {
    throw new Error("TELEMETRY_SCENE_INDEX_CAPACITY_INVALID");
  }
  assertUnique("FILE_EDGE", canonicalNodeIds);
  const partitions: string[][] = [];
  for (let index = 0; index < canonicalNodeIds.length; index += capacity) {
    partitions.push(canonicalNodeIds.slice(index, index + capacity));
  }
  return partitions;
}

function shellId(
  input: TelemetrySceneIndexInput,
  shellClass: TelemetrySceneShellClass,
  canonicalPartitionIndex: number | null,
) {
  const rootKey = stableHex(`${input.brainId}|${input.activeRootOrFolderId}`);
  const snapshotKey = input.topologySnapshotHash.slice(0, 16).toLowerCase();
  const suffix = canonicalPartitionIndex === null ? "authority" : `partition-${canonicalPartitionIndex + 1}`;
  return `t023-scene-shell:${snapshotKey}:${rootKey}:${shellClass}:${suffix}`;
}

function shellRadiusRatio(sceneLevelIndex: number, totalShells: number) {
  if (totalShells <= 1) return 1;
  const terminalRatio = 0.28;
  return 1 - (sceneLevelIndex / (totalShells - 1)) * (1 - terminalRatio);
}

function nodePositions(shellIdentity: string, nodeIds: string[]) {
  const phase = stableUnit(shellIdentity) * Math.PI * 2;
  return Object.fromEntries(nodeIds.map((nodeId, index) => [
    nodeId,
    canonicalSpherePoint(index, nodeIds.length, phase),
  ])) as Record<string, TelemetryScenePoint3>;
}

export function materializeTelemetrySceneIndex(input: TelemetrySceneIndexInput): TelemetrySceneIndex {
  const brainId = input.brainId.trim();
  const topologySnapshotHash = input.topologySnapshotHash.trim().toLowerCase();
  const rootId = input.activeRootOrFolderId.trim();
  if (!brainId || !rootId) throw new Error("TELEMETRY_SCENE_INDEX_AUTHORITY_REQUIRED");
  if (!/^[a-f0-9]{64}$/.test(topologySnapshotHash)) {
    throw new Error("TELEMETRY_SCENE_INDEX_SNAPSHOT_HASH_REQUIRED");
  }
  assertUnique("CHILD_FOLDER", input.childFolderNodeIds);
  assertUnique("FILE_EDGE", input.fileEdgeNodeIds);
  const allIds = [rootId, ...input.childFolderNodeIds, ...input.fileEdgeNodeIds];
  assertUnique("CROSS_CLASS", allIds);

  const fileEdgePartitions = partitionTelemetryFileEdgeNodeIds(input.fileEdgeNodeIds);
  const fixedOuterId = shellId(input, "FIXED_OUTER_FOLDER_NODE_ORB", null);
  const terminalRootId = shellId(input, "TERMINAL_ROOT_FOLDER_BUTTON_ORB", null);
  const unorderedShells: Array<{
    shell_id: string;
    shell_class: TelemetrySceneShellClass;
    node_ids: string[];
    capacity: number | null;
    canonical_partition_index: number | null;
  }> = [{
    shell_id: fixedOuterId,
    shell_class: "FIXED_OUTER_FOLDER_NODE_ORB",
    node_ids: [...input.childFolderNodeIds],
    capacity: null,
    canonical_partition_index: null,
  }];

  // Canonical partition 0 is closest to the terminal root. Scene travel is
  // indexed outer-to-inner, so overflow partitions are inserted in reverse
  // scene order without changing membership or canonical ordering.
  for (let index = fileEdgePartitions.length - 1; index >= 0; index -= 1) {
    unorderedShells.push({
      shell_id: shellId(input, "FILE_EDGE_NODE_ORB", index),
      shell_class: "FILE_EDGE_NODE_ORB",
      node_ids: [...fileEdgePartitions[index]],
      capacity: TELEMETRY_FILE_EDGE_CAPACITY,
      canonical_partition_index: index,
    });
  }
  unorderedShells.push({
    shell_id: terminalRootId,
    shell_class: "TERMINAL_ROOT_FOLDER_BUTTON_ORB",
    node_ids: [rootId],
    capacity: 1,
    canonical_partition_index: null,
  });

  const totalShells = unorderedShells.length;
  const shells = unorderedShells.map((shell, sceneLevelIndex): TelemetrySceneShellIndex => ({
    brain_id: brainId,
    topology_snapshot_hash: topologySnapshotHash,
    active_root_or_folder_id: rootId,
    scene_level_index: sceneLevelIndex,
    shell_id: shell.shell_id,
    shell_class: shell.shell_class,
    parent_shell_id: sceneLevelIndex > 0 ? unorderedShells[sceneLevelIndex - 1].shell_id : null,
    child_shell_id: sceneLevelIndex < totalShells - 1 ? unorderedShells[sceneLevelIndex + 1].shell_id : null,
    node_ids: [...shell.node_ids],
    node_count: shell.node_ids.length,
    capacity: shell.capacity,
    canonical_order: [...shell.node_ids],
    canonical_partition_index: shell.canonical_partition_index,
    radius_ratio: shellRadiusRatio(sceneLevelIndex, totalShells),
    depth_position: sceneLevelIndex,
    active: sceneLevelIndex === 0,
    saved_orientation: [0, 0, 0],
    selected_brain_color_reference: input.selectedBrainColorReference,
    node_positions: nodePositions(shell.shell_id, shell.node_ids),
    glass_volume_transparent: true,
    selected_color_application: "OUTER_RIM_ONLY",
    connector_line_count: 0,
    active_presentation_law: "CAMERA_DOLLY_COMMON_ENVELOPE_NO_MESH_SCALE_POP",
    back_layer_presence_law: "OUTER_SHELLS_RETAINED_BEHIND_ACTIVE",
    reflection_motion_law: "SHELL_LOCAL_HIGHLIGHTS_ROTATE_WITH_ORB",
  }));

  const cacheKey = [brainId, topologySnapshotHash, rootId].join("|");
  return {
    schema: "T023_DERIVED_SAVED_TOPOLOGY_SCENE_INDEX_V1",
    cache_key: cacheKey,
    brain_id: brainId,
    topology_snapshot_hash: topologySnapshotHash,
    active_root_or_folder_id: rootId,
    file_edge_capacity: TELEMETRY_FILE_EDGE_CAPACITY,
    file_edge_count: input.fileEdgeNodeIds.length,
    child_folder_count: input.childFolderNodeIds.length,
    file_edge_partition_distribution: fileEdgePartitions.map((partition) => partition.length),
    canonical_file_edge_node_ids: [...input.fileEdgeNodeIds],
    canonical_child_folder_node_ids: [...input.childFolderNodeIds],
    ordered_shell_ids: shells.map((shell) => shell.shell_id),
    total_glass_orb_count: shells.length,
    scene_transition_count: Math.max(0, shells.length - 1),
    terminal_root_shell_id: terminalRootId,
    fixed_outer_folder_shell_id: fixedOuterId,
    selected_brain_color_reference: input.selectedBrainColorReference,
    shells,
    raw_project_files_reread: 0,
    canonical_project_truth_mutated: false,
  };
}

export function getOrCreateTelemetrySceneIndex(input: TelemetrySceneIndexInput): TelemetrySceneIndexResolution {
  const cacheKey = [
    input.brainId.trim(),
    input.topologySnapshotHash.trim().toLowerCase(),
    input.activeRootOrFolderId.trim(),
  ].join("|");
  const cached = sceneIndexCache.get(cacheKey);
  if (cached) return { index: cached, cacheStatus: "HIT" };
  const index = materializeTelemetrySceneIndex(input);
  sceneIndexCache.set(cacheKey, index);
  return { index, cacheStatus: "MATERIALIZED" };
}

export function clearTelemetrySceneIndexCacheForTests() {
  sceneIndexCache.clear();
}
