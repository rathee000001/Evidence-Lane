import { Component, useEffect, useMemo, useRef, useState, type CSSProperties, type ErrorInfo, type ReactNode } from "react";
import { Html, useTexture } from "@react-three/drei";
import { Canvas, useFrame, useThree, type ThreeEvent } from "@react-three/fiber";
import {
  AdditiveBlending,
  BackSide,
  DoubleSide,
  Group,
  MathUtils,
  Quaternion,
  SRGBColorSpace,
  Vector3,
  type Texture,
} from "three";
import { T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1 } from "../assets/telemetry/T023TelemetrySceneAssets";
import type { TelemetryGraphResult, TelemetryNode, TelemetryRelationEdge, TelemetryTone } from "../simulation/BrainTelemetryContract";
import {
  TELEMETRY_FILE_EDGE_CAPACITY,
  getOrCreateTelemetrySceneIndex,
  type TelemetrySceneIndex,
  type TelemetrySceneShellIndex,
} from "../simulation/TelemetrySceneIndex";

type Point3 = [number, number, number];
type TelemetryVisualRole = "rootFolderNode" | "folderNode" | "fileLeafNode" | "supportNode";
type OrbFocusDirection = "INWARD" | "OUTWARD";

type OrbMember = {
  node: TelemetryNode;
  role: Exclude<TelemetryVisualRole, "rootFolderNode">;
  unitPosition: Point3;
  defaultLabelVisible: boolean;
};

type DataDerivedOrbLayer = {
  orbId: string;
  layerIndex: number;
  homeDepthSlot: number;
  semanticRole: "FIXED_OUTER_FOLDER_NODE_ORB" | "EDGE_NODE_ORB";
  sceneShell: TelemetrySceneShellIndex;
  members: OrbMember[];
  connectorLineCount: 0;
  geometryLaw: "NO_TOPOLOGY_CONNECTOR_LINES_USER_DEFERRED";
};

type TelemetryOrbLayout = {
  root: TelemetryNode | null;
  layers: DataDerivedOrbLayer[];
  outerRadius: number;
  sceneIndex: TelemetrySceneIndex | null;
  sceneIndexCacheStatus: "HIT" | "MATERIALIZED" | "UNAVAILABLE";
};

type PointerCaptureTarget = EventTarget & {
  setPointerCapture?: (pointerId: number) => void;
  releasePointerCapture?: (pointerId: number) => void;
};

export type TelemetryOrbFocusRequest = {
  orbId: string;
  nextFocusedOrbId: string;
  direction: OrbFocusDirection;
  nonce: number;
};

export type TelemetryOrbFocusAudit = {
  focusedOrbId: string;
  direction: OrbFocusDirection;
  focusedLayerIndexBefore: number;
  focusedLayerIndexAfter: number;
  nestedFocusScale: number;
  outerWrapScale: number;
  moved: boolean;
  hoveredAnchorPreserved: boolean;
  orientationPreserved: true;
  inactiveLayersRotationLocked: true;
  cameraSceneTravel: true;
  orbPopScaleMutation: 0;
  zDepthOffset: number;
  focusLaw: "CAMERA_SCENE_POINT_A_TO_POINT_B_NESTED_ORB_TRAVERSAL";
  sourceReadsDuringFocus: number;
};

export type TelemetryOrbRotationAudit = {
  orbId: string;
  rotationBefore: Point3;
  rotationAfter: Point3;
  dragDeltaX: number;
  dragDeltaY: number;
  dragDeltaZ: number;
  otherLayersChanged: 0;
  sourceReadsDuringNavigation: number;
};

export type TelemetryOrbLayoutAudit = {
  rootNodeId: string;
  savedMembershipCount: number;
  orbCount: number;
  distribution: number[];
  focusStopCount: number;
  folderOrbCount: 1;
  edgeOrbCount: number;
  edgeMembersPerOrb: 20;
  fileEdgeNodeCount: number;
  childFolderNodeCount: number;
  fileEdgeDistribution: number[];
  sceneDistribution: number[];
  sceneTransitionCount: number;
  orderedShellIds: string[];
  sceneIndexCacheKey: string;
  sceneIndexCacheStatus: "HIT" | "MATERIALIZED";
  topologySnapshotHash: string;
  canonicalFileEdgeNodeIds: string[];
  canonicalChildFolderNodeIds: string[];
  maxFileEdgeShellPopulation: number;
  nodeLossCount: 0;
  nodeDuplicateCount: 0;
  terminalRootOrbId: string;
  terminalRootLayerIndex: number;
  terminalRootNonRotating: true;
  sourceReadsDuringLayout: number;
};

type BrainTelemetrySceneProps = {
  graph: TelemetryGraphResult;
  overlayEnabled: boolean;
  selectedNodeId: string;
  selectedEdgeId: string;
  cameraDistance: number;
  cameraResetNonce: number;
  focusRequest: TelemetryOrbFocusRequest | null;
  focusedOrbId: string | null;
  onFocusAudit: (audit: TelemetryOrbFocusAudit) => void;
  onRotationAudit: (audit: TelemetryOrbRotationAudit) => void;
  onLayoutAudit: (audit: TelemetryOrbLayoutAudit) => void;
  onHoveredOrbChange: (orbId: string | null) => void;
  reducedMotion: boolean;
  brainColor: string;
  onSelectScene: () => void;
  onSelectNode: (node: TelemetryNode) => void;
  onSelectEdge: (edge: TelemetryRelationEdge) => void;
  onOpenFolder: (node: TelemetryNode) => void;
  onOpenTarget: (node: TelemetryNode) => void;
};

const GREEN = "#43d99a";
const RED = "#ff6076";
const GOLD = "#efd28a";
const TELEMETRY_LAYOUT_FOV = 42;
const TELEMETRY_LAYOUT_DISTANCE = 19;
const TELEMETRY_ROOT_BUTTON_RADIUS = 1.28;
const ORB_IDLE_CURSOR = { cursor: "grab" } as const;
const ORB_ACTIVE_CURSOR = { cursor: "grabbing" } as const;

function shellOrientationStorageKey(shell: TelemetrySceneShellIndex) {
  return `t023.telemetry.scene-orientation:${shell.topology_snapshot_hash}:${shell.shell_id}`;
}

function loadShellOrientation(shell: TelemetrySceneShellIndex): Point3 {
  if (typeof window === "undefined") return shell.saved_orientation;
  try {
    const parsed = JSON.parse(window.localStorage.getItem(shellOrientationStorageKey(shell)) || "null");
    if (Array.isArray(parsed) && parsed.length === 3 && parsed.every(Number.isFinite)) {
      return [Number(parsed[0]), Number(parsed[1]), Number(parsed[2])];
    }
  } catch { /* deterministic saved default below */ }
  return shell.saved_orientation;
}

function persistShellOrientation(shell: TelemetrySceneShellIndex, orientation: Point3) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(shellOrientationStorageKey(shell), JSON.stringify(orientation));
  } catch { /* visual runtime persistence must never mutate or block topology */ }
}

function isFolderLike(node: TelemetryNode) {
  return node.kind === "folder" || node.kind === "source";
}

function displayNodeLabel(node: TelemetryNode) {
  if (node.kind === "brain") return node.label;
  const normalized = String(node.relative_path || node.label).replaceAll("\\", "/");
  return normalized.split("/").filter(Boolean).at(-1) || node.label;
}

export function buildTelemetryNeighborhood(graph: TelemetryGraphResult): TelemetryGraphResult {
  const byId = new Map(graph.nodes.map((node) => [node.node_id, node]));
  const scopedRoot = graph.nodes.find((node) => node.node_id === graph.scope.scope_id);
  const brainRoot = graph.nodes.find((node) => node.kind === "brain");
  const sourceRoots = graph.nodes.filter((node) => node.kind === "source");
  const sourceRoot = sourceRoots.sort((left, right) => {
    const childCount = (nodeId: string) => graph.edges.filter((edge) => {
      const target = byId.get(edge.to_node_id);
      return edge.from_node_id === nodeId && (target?.kind === "folder" || target?.kind === "file");
    }).length;
    return childCount(right.node_id) - childCount(left.node_id)
      || displayNodeLabel(left).localeCompare(displayNodeLabel(right));
  })[0];
  const root = scopedRoot
    || sourceRoot
    || brainRoot
    || graph.nodes.find((node) => node.kind === "folder")
    || graph.nodes[0];
  if (!root) return graph;

  const outgoing = graph.edges.filter((edge) => edge.from_node_id === root.node_id);
  const sourceIds = new Set(outgoing
    .filter((edge) => byId.get(edge.to_node_id)?.kind === "source")
    .map((edge) => edge.to_node_id));
  const folderFileEdges = outgoing.filter((edge) => {
    const target = byId.get(edge.to_node_id);
    return target?.kind === "folder" || target?.kind === "file";
  });
  const flattenedSourceEdges = root.kind === "brain" ? graph.edges.filter((edge) => {
    const target = byId.get(edge.to_node_id);
    return sourceIds.has(edge.from_node_id) && (target?.kind === "folder" || target?.kind === "file");
  }) : [];
  const candidateEdges = folderFileEdges.length || flattenedSourceEdges.length
    ? [...folderFileEdges, ...flattenedSourceEdges]
    : outgoing.filter((edge) => isFolderLike(byId.get(edge.to_node_id) || root));
  const candidateByTarget = new Map<string, TelemetryRelationEdge>();
  candidateEdges.forEach((edge) => {
    if (!candidateByTarget.has(edge.to_node_id)) candidateByTarget.set(edge.to_node_id, edge);
  });
  const directChildren = Array.from(candidateByTarget.entries())
    .map(([nodeId, edge]) => ({ node: byId.get(nodeId), edge }))
    .filter((entry): entry is { node: TelemetryNode; edge: TelemetryRelationEdge } => Boolean(entry.node));
  // The current root plus every immediate indexed folder/file is the visual
  // truth. The former 32-neighbor slice hid real saved membership, so this
  // presentation never caps a valid neighborhood or rereads source files.
  const visibleEntries = directChildren;
  const visibleChildren = visibleEntries.map(({ node }) => ({ ...node, label: displayNodeLabel(node) }));
  const rootView = { ...root, label: displayNodeLabel(root) };
  const visibleIds = new Set([root.node_id, ...visibleChildren.map((node) => node.node_id)]);
  const rootConnections = visibleEntries.map(({ edge }) => edge.from_node_id === root.node_id ? edge : ({
    ...edge,
    edge_id: `view:${root.node_id}:${edge.edge_id}`,
    from_node_id: root.node_id,
  }));
  const crossConnections = graph.edges.filter((edge) => (
    visibleIds.has(edge.from_node_id) && visibleIds.has(edge.to_node_id)
  ));
  const visibleEdges = Array.from(new Map(
    [...rootConnections, ...crossConnections].map((edge) => [edge.edge_id, edge]),
  ).values());
  const visibleNodes = [rootView, ...visibleChildren];
  return {
    ...graph,
    nodes: visibleNodes,
    edges: visibleEdges,
    counts: {
      ...graph.counts,
      nodes_returned: visibleNodes.length,
      nodes_total_in_scope: directChildren.length + 1,
      edges_returned: visibleEdges.length,
    },
    truncated: false,
    max_nodes: 0,
  };
}

function rootNodeFor(graph: TelemetryGraphResult) {
  const scoped = graph.scope.scope_id
    ? graph.nodes.find((node) => node.node_id === graph.scope.scope_id)
    : undefined;
  return scoped
    || graph.nodes.find((node) => node.kind === "brain")
    || graph.nodes.find((node) => node.kind === "source")
    || graph.nodes.find((node) => node.kind === "folder")
    || graph.nodes[0]
    || null;
}

export function buildDataDerivedOrbLayers(
  graph: TelemetryGraphResult,
  outerRadius: number,
  selectedBrainColorReference = "#67d9f6",
): TelemetryOrbLayout {
  const root = rootNodeFor(graph);
  if (!root) return {
    root: null,
    layers: [],
    outerRadius,
    sceneIndex: null,
    sceneIndexCacheStatus: "UNAVAILABLE",
  };
  const nodeById = new Map(graph.nodes.map((node) => [node.node_id, node]));
  const childFolderNodeIds = graph.nodes
    .filter((node) => node.node_id !== root.node_id && isFolderLike(node))
    .map((node) => node.node_id);
  const fileEdgeNodeIds = graph.nodes
    .filter((node) => node.node_id !== root.node_id && !isFolderLike(node))
    .map((node) => node.node_id);
  const resolution = getOrCreateTelemetrySceneIndex({
    brainId: graph.brain_name,
    topologySnapshotHash: String(graph.snapshot.snapshot_hash || ""),
    activeRootOrFolderId: root.node_id,
    childFolderNodeIds,
    fileEdgeNodeIds,
    selectedBrainColorReference,
  });
  const topologyShells = resolution.index.shells.filter(
    (shell) => shell.shell_class !== "TERMINAL_ROOT_FOLDER_BUTTON_ORB",
  );
  const layers = topologyShells.map((shell): DataDerivedOrbLayer => ({
    orbId: shell.shell_id,
    layerIndex: shell.scene_level_index,
    homeDepthSlot: resolution.index.total_glass_orb_count - shell.scene_level_index,
    semanticRole: shell.shell_class === "FIXED_OUTER_FOLDER_NODE_ORB"
      ? "FIXED_OUTER_FOLDER_NODE_ORB"
      : "EDGE_NODE_ORB",
    sceneShell: shell,
    members: shell.node_ids.map((nodeId) => {
      const node = nodeById.get(nodeId);
      if (!node) throw new Error("TELEMETRY_SCENE_INDEX_NODE_NOT_FOUND");
      return {
        node,
        role: isFolderLike(node)
          ? "folderNode" as const
          : node.kind === "file"
            ? "fileLeafNode" as const
            : "supportNode" as const,
        unitPosition: shell.node_positions[nodeId],
        defaultLabelVisible: shell.scene_level_index === 0,
      };
    }),
    connectorLineCount: 0,
    geometryLaw: "NO_TOPOLOGY_CONNECTOR_LINES_USER_DEFERRED",
  }));
  return {
    root,
    layers,
    outerRadius,
    sceneIndex: resolution.index,
    sceneIndexCacheStatus: resolution.cacheStatus,
  };
}

function effectiveTone(tone: TelemetryTone, overlayEnabled: boolean, directEvidence: boolean | undefined): TelemetryTone {
  if (!overlayEnabled || directEvidence !== true) return "NEUTRAL";
  return tone;
}

function toneColor(tone: TelemetryTone, neutral: string) {
  if (tone === "GREEN") return GREEN;
  if (tone === "RED") return RED;
  return neutral;
}

function layerEvidenceTone(
  layer: DataDerivedOrbLayer,
  graph: TelemetryGraphResult,
  overlayEnabled: boolean,
): TelemetryTone {
  if (!overlayEnabled) return "NEUTRAL";
  const memberIds = new Set(layer.members.map(({ node }) => node.node_id));
  const tones: TelemetryTone[] = layer.members
    .filter(({ node }) => node.overlay_evidence_direct === true)
    .map(({ node }) => node.tone);
  graph.edges.forEach((edge) => {
    if (edge.overlay_evidence_direct === true
      && (memberIds.has(edge.from_node_id) || memberIds.has(edge.to_node_id))) tones.push(edge.tone);
  });
  if (tones.includes("RED")) return "RED";
  if (tones.includes("GREEN")) return "GREEN";
  return "NEUTRAL";
}

function HumanEyeLensGlass({
  radius,
  tone,
  authorityId,
  buttonRole,
  brainColor,
  neonRim = false,
  interactionSurface = true,
  contextOpacity = 1,
  rimEmphasis = 1,
  children,
}: {
  radius: number;
  tone: TelemetryTone;
  authorityId: string;
  buttonRole: "ROOT_NODE_BUTTON" | "FOLDER_NODE_BUTTON" | "FILE_EDGE_BUTTON" | "TOPOLOGY_LAYER_LENS";
  brainColor?: string;
  neonRim?: boolean;
  interactionSurface?: boolean;
  contextOpacity?: number;
  rimEmphasis?: number;
  children?: ReactNode;
}) {
  const evidenceColor = tone === "GREEN" ? "#7bf1bd" : tone === "RED" ? "#ff879a" : "#bfeef7";
  const isTopologyLayer = buttonRole === "TOPOLOGY_LAYER_LENS";
  const shellColor = isTopologyLayer ? "#effcff" : evidenceColor;
  const boundedContextOpacity = MathUtils.clamp(contextOpacity, 0.08, 1);
  const outerOpacity = (isTopologyLayer ? 0.055 : 0.23) * boundedContextOpacity;
  const innerOpacity = (isTopologyLayer ? 0.045 : 0.11) * boundedContextOpacity;
  const cornealOpacity = (isTopologyLayer ? 0.075 : 0.17) * boundedContextOpacity;
  return <group
    name="humanEyeLensGlass"
    userData={{
      authorityId,
      buttonRole,
      geometryAuthority: "PROCEDURAL_THREE_DIMENSIONAL_GLASS_WITH_LIGHTWEIGHT_ALPHA_CONTENT",
      shellLaw: "HUMAN_EYE_LENS_OUTER_CORNEA_INNER_VOLUME_REFRACTIVE_THICKNESS",
      glareLaw: "CURVED_CORNEAL_HIGHLIGHT_AND_PRISMATIC_CAUSTIC",
      reflectionMotionLaw: "SHELL_LOCAL_GLARE_CAUSTIC_AND_ABSORPTION_ROTATE_WITH_ORB_TRANSFORM",
      staticViewportShadowPatch: false,
      neonRimLaw: neonRim ? "EXTERNAL_FIXED_2D_RIM_ONLY_GLASS_VOLUME_NEUTRAL" : "NO_NEON_RIM_INNER_OR_ROOT_ORB",
      requestedBrainRimColor: neonRim ? brainColor || "#77dcf5" : "NONE",
      contextOpacity: boundedContextOpacity,
      rimEmphasis,
      glassVolumeColorLaw: "NEUTRAL_TRANSPARENT_NEVER_SELECTED_BRAIN_TINT",
      outerRadius: radius,
      innerRadius: radius * 0.82,
      runtimeRasterAsset: "LIGHTWEIGHT_TRANSPARENT_ALPHA_CONTENT",
    }}
  >
    <mesh renderOrder={0} raycast={interactionSurface ? undefined : () => undefined}>
      <sphereGeometry args={[radius, 64, 48]} />
      <meshPhysicalMaterial
        color={shellColor}
        emissive="#000000"
        emissiveIntensity={0}
        transparent
        opacity={outerOpacity}
        transmission={isTopologyLayer ? 0.98 : 0.88}
        thickness={radius * 0.62}
        ior={1.49}
        roughness={0.035}
        metalness={0}
        clearcoat={1}
        clearcoatRoughness={0.025}
        side={DoubleSide}
        depthWrite={false}
      />
    </mesh>
    <mesh scale={radius * 0.82} renderOrder={1} raycast={interactionSurface ? undefined : () => undefined}>
      <sphereGeometry args={[1, 56, 40]} />
      <meshPhysicalMaterial
        color="#d9f7fc"
        emissive="#000000"
        emissiveIntensity={0}
        transparent
        opacity={innerOpacity}
        transmission={0.94}
        thickness={radius * 0.24}
        ior={1.41}
        roughness={0.045}
        clearcoat={1}
        side={BackSide}
        depthWrite={false}
      />
    </mesh>
    <mesh position={[0, 0, radius * 0.58]} scale={[radius * 0.83, radius * 0.83, radius * 0.22]} renderOrder={3} raycast={interactionSurface ? undefined : () => undefined}>
      <sphereGeometry args={[1, 48, 32]} />
      <meshPhysicalMaterial color="#d8fbff" emissive="#000000" emissiveIntensity={0} transparent opacity={cornealOpacity} transmission={0.92} thickness={radius * 0.18} ior={1.51} roughness={0.025} clearcoat={1} clearcoatRoughness={0.02} depthWrite={false} />
    </mesh>
    <mesh position={[-radius * 0.27, radius * 0.3, radius * 0.84]} scale={[radius * 0.3, radius * 0.13, radius * 0.035]} renderOrder={9} raycast={interactionSurface ? undefined : () => undefined}>
      <sphereGeometry args={[1, 30, 18]} />
      <meshPhysicalMaterial color="#ffffff" emissive="#000000" emissiveIntensity={0} transparent opacity={(isTopologyLayer ? 0.42 : 0.62) * boundedContextOpacity * rimEmphasis} transmission={0.22} roughness={0.02} clearcoat={1} depthWrite={false} />
    </mesh>
    <mesh position={[radius * 0.3, -radius * 0.28, radius * 0.79]} scale={[radius * 0.17, radius * 0.075, radius * 0.025]} renderOrder={8} raycast={interactionSurface ? undefined : () => undefined}>
      <sphereGeometry args={[1, 24, 14]} />
      <meshPhysicalMaterial color="#f4d99b" emissive="#000000" emissiveIntensity={0} transparent opacity={0.28 * boundedContextOpacity} transmission={0.22} roughness={0.03} clearcoat={1} depthWrite={false} />
    </mesh>
    <mesh rotation={[0.42, -0.58, -0.76]} position={[0, 0, radius * 0.08]} renderOrder={7} raycast={() => undefined}>
      <torusGeometry args={[radius * 0.74, Math.max(0.008, radius * 0.012), 8, 96, Math.PI * 0.78]} />
      <meshBasicMaterial color="#dffbff" transparent opacity={0.34 * boundedContextOpacity * rimEmphasis} depthWrite={false} toneMapped={false} blending={AdditiveBlending} />
    </mesh>
    <mesh position={[radius * 0.28, -radius * 0.21, -radius * 0.54]} scale={[radius * 0.42, radius * 0.23, radius * 0.08]} renderOrder={2} raycast={() => undefined}>
      <sphereGeometry args={[1, 30, 18]} />
      <meshBasicMaterial color="#3c6f7c" transparent opacity={0.07 * boundedContextOpacity} depthWrite={false} side={DoubleSide} />
    </mesh>
    {children}
  </group>;
}

function FixedOuterNeonRim({ radius, color }: { radius: number; color: string }) {
  return <group
    name="fixedOuterNeonRim2D"
    userData={{
      presentation: "FIXED_TWO_DIMENSIONAL_OUTER_RIM",
      colorLaw: "SELECTED_BRAIN_COLOR_RIM_ONLY",
      glassVolumeTint: "NONE",
      rotationLocked: true,
      neonLaw: "THREE_STAGE_ADDITIVE_RIM_CORE_HALO_AND_SOFT_BLOOM",
    }}
  >
    <mesh position={[0, 0, 0.04]} renderOrder={15} raycast={() => undefined}>
      <ringGeometry args={[radius * 1.002, radius * 1.016, 128]} />
      <meshBasicMaterial color={color} transparent opacity={0.96} side={DoubleSide} depthWrite={false} depthTest={false} toneMapped={false} blending={AdditiveBlending} />
    </mesh>
    <mesh position={[0, 0, 0.035]} renderOrder={14} raycast={() => undefined}>
      <ringGeometry args={[radius * 0.994, radius * 1.024, 128]} />
      <meshBasicMaterial color={color} transparent opacity={0.24} side={DoubleSide} depthWrite={false} depthTest={false} toneMapped={false} blending={AdditiveBlending} />
    </mesh>
    <mesh position={[0, 0, 0.03]} renderOrder={13} raycast={() => undefined}>
      <ringGeometry args={[radius * 0.982, radius * 1.04, 128]} />
      <meshBasicMaterial color={color} transparent opacity={0.075} side={DoubleSide} depthWrite={false} depthTest={false} toneMapped={false} blending={AdditiveBlending} />
    </mesh>
  </group>;
}

function useTelemetrySceneTexture(url: string): Texture {
  const texture = useTexture(url);
  useEffect(() => {
    texture.colorSpace = SRGBColorSpace;
    texture.needsUpdate = true;
  }, [texture]);
  return texture;
}

function LightweightTelemetryAssetSprite({
  url,
  size,
  opacity = 1,
  tint = "#ffffff",
  renderOrder = 7,
}: {
  url: string;
  size: number;
  opacity?: number;
  tint?: string;
  renderOrder?: number;
}) {
  const texture = useTelemetrySceneTexture(url);
  return <sprite scale={[size, size, 1]} renderOrder={renderOrder} raycast={() => undefined}>
    <spriteMaterial
      map={texture}
      color={tint}
      transparent
      opacity={opacity}
      alphaTest={0.025}
      depthWrite={false}
      toneMapped={false}
    />
  </sprite>;
}

function TopologyLayerLens({
  layer,
  tone,
  brainColor,
  active,
  contextOpacity,
}: {
  layer: DataDerivedOrbLayer;
  tone: TelemetryTone;
  brainColor: string;
  active: boolean;
  contextOpacity: number;
}) {
  const outermost = layer.layerIndex === 0;
  return <group
    name="topologyLayerLensWithoutConnectors"
    userData={{
      presentation: "DATA_DERIVED_OWNING_GLASS_ORB",
      generatedFrom: "OPENED_FOLDER_SAVED_MEMBERSHIP",
      geometryLaw: layer.geometryLaw,
      connectorLineCount: 0,
      connectorLaw: "REMOVED_USER_WILL_SUPPLY_LATER",
      orbId: layer.orbId,
      layerPopulation: layer.members.length,
      overlayTone: tone,
      active,
      contextOpacity,
      outermost,
      neonRimLaw: outermost ? "SELECTED_BRAIN_COLOR_OUTERMOST_ONLY" : "NO_NEON_RIM",
    }}
  >
    <HumanEyeLensGlass
      radius={1.045}
      tone={tone}
      authorityId={layer.orbId}
      buttonRole="TOPOLOGY_LAYER_LENS"
      brainColor={brainColor}
      neonRim={false}
      contextOpacity={contextOpacity}
      rimEmphasis={active ? 1.28 : 0.72}
    />
    {outermost && <LightweightTelemetryAssetSprite
      url={T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.universalGlassOrb.url}
      size={2.075}
      opacity={0.035 * contextOpacity}
      tint="#ffffff"
      renderOrder={2}
    />}
  </group>;
}

function LightweightTelemetryButton({
  node,
  role,
  owningOrbId,
  tone,
  brainColor,
  selected,
  contextOpacity,
}: {
  node: TelemetryNode;
  role: TelemetryVisualRole;
  owningOrbId: string;
  tone: TelemetryTone;
  brainColor: string;
  selected: boolean;
  contextOpacity: number;
}) {
  if (role === "rootFolderNode") return <group
    name="telemetryLightweightRootNodeButton"
    scale={1}
    userData={{
      presentation: "LIGHTWEIGHT_ROOT_NODE_ALPHA_ASSET_INSIDE_REAL_3D_GLASS_ORB",
      runtimeRasterAsset: T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.rootNode.sha256,
      glbLaw: "REFERENCE_ONLY_ROOT_GLB_EXCLUDED_FOR_LIGHTWEIGHT_RUNTIME",
      electromagneticPulseLaw: "DEFERRED_TO_SEPARATE_ANIMATION_TASK",
      identityLaw: "STANDALONE_ROOT_IDENTITY_CLUSTER_NOT_TOPOLOGY_CONNECTOR",
      sizeLaw: "ROOT_LARGEST_FOLDER_NODE_MEDIUM_FILE_EDGE_SMALLEST",
      buttonRole: "ROOT_NODE_BUTTON",
      selected,
      contextOpacity,
    }}
  >
    <HumanEyeLensGlass radius={TELEMETRY_ROOT_BUTTON_RADIUS} tone={tone} authorityId={node.node_id} buttonRole="ROOT_NODE_BUTTON" brainColor={brainColor} neonRim={false} contextOpacity={contextOpacity}>
      <LightweightTelemetryAssetSprite
        url={T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.rootNode.url}
        size={1.82}
        opacity={0.96 * contextOpacity}
        tint="#ffffff"
      />
    </HumanEyeLensGlass>
  </group>;
  if (role === "folderNode") return <group
    name="telemetryLightweightFolderNodeButton"
    scale={1}
    userData={{
      nodeId: node.node_id,
      owningOrbId,
      presentation: "LIGHTWEIGHT_TRANSPARENT_FOLDER_CRYSTAL_BUTTON",
      runtimeRasterAsset: T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.folderNode.sha256,
      scaleLaw: "FOLDER_AND_FILE_MATCH_ICON_ORB_LEVEL",
      ratioLaw: "FOLDER_NODE_2_65X_FILE_VISUAL_HEIGHT",
      buttonRole: "FOLDER_NODE_BUTTON",
      selected,
      contextOpacity,
    }}
  >
    <HumanEyeLensGlass radius={0.43} tone={tone} authorityId={node.node_id} buttonRole="FOLDER_NODE_BUTTON" brainColor={brainColor} neonRim={false} interactionSurface={false} contextOpacity={contextOpacity}>
      <LightweightTelemetryAssetSprite
        url={T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.folderNode.url}
        size={0.78}
        opacity={0.98 * contextOpacity}
      />
    </HumanEyeLensGlass>
  </group>;
  if (role === "fileLeafNode") return <group
    name="telemetryLightweightFileEdgeButton"
    scale={1}
    userData={{
      nodeId: node.node_id,
      owningOrbId,
      presentation: "LIGHTWEIGHT_TRANSPARENT_FILE_EDGE_CRYSTAL_BUTTON",
      runtimeRasterAsset: T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.edgeNode.sha256,
      scaleLaw: "FOLDER_AND_FILE_MATCH_ICON_ORB_LEVEL",
      buttonRole: "FILE_EDGE_BUTTON",
      selected,
      contextOpacity,
    }}
  >
    <HumanEyeLensGlass radius={0.205} tone={tone} authorityId={node.node_id} buttonRole="FILE_EDGE_BUTTON" brainColor={brainColor} neonRim={false} interactionSurface={false} contextOpacity={contextOpacity}>
      <LightweightTelemetryAssetSprite
        url={T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.edgeNode.url}
        size={0.36}
        opacity={0.98 * contextOpacity}
      />
    </HumanEyeLensGlass>
  </group>;
  return <LightweightTelemetryAssetSprite
    url={T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.edgeNode.url}
    size={0.29}
    opacity={0.92 * contextOpacity}
  />;
}

function TelemetryNodeMesh({
  node,
  role,
  position,
  owningOrbId,
  inverseOrbScale,
  defaultLabelVisible,
  activeShell,
  contextOpacity,
  overlayEnabled,
  selected,
  onSelect,
  onOpenFolder,
  onOpenTarget,
  reducedMotion,
  depthSlot,
  brainColor,
}: {
  node: TelemetryNode;
  role: TelemetryVisualRole;
  position: Point3;
  owningOrbId: string;
  inverseOrbScale: number;
  defaultLabelVisible: boolean;
  activeShell: boolean;
  contextOpacity: number;
  overlayEnabled: boolean;
  selected: boolean;
  onSelect: (node: TelemetryNode) => void;
  onOpenFolder: (node: TelemetryNode) => void;
  onOpenTarget: (node: TelemetryNode) => void;
  reducedMotion: boolean;
  depthSlot: number;
  brainColor: string;
}) {
  const { camera } = useThree();
  const clickTimerRef = useRef<number | null>(null);
  const pillDepthGroupRef = useRef<Group>(null);
  const parentQuaternionRef = useRef(new Quaternion());
  const cameraNormalRef = useRef(new Vector3());
  const pillTargetPositionRef = useRef(new Vector3());
  const [hovered, setHovered] = useState(false);
  const [pressed, setPressed] = useState(false);
  useEffect(() => () => {
    if (clickTimerRef.current !== null) window.clearTimeout(clickTimerRef.current);
  }, []);
  const tone = effectiveTone(node.tone, overlayEnabled, node.overlay_evidence_direct);
  const nodeRadius = role === "rootFolderNode" ? 1.38 : role === "folderNode" ? 0.5 : role === "fileLeafNode" ? 0.23 : 0.18;
  useFrame((_state, delta) => {
    const group = pillDepthGroupRef.current;
    if (!group) return;
    const activeHover = activeShell && hovered;
    const lift = pressed ? nodeRadius * 0.045 : activeHover ? nodeRadius * 0.16 : 0;
    const scaleTarget = pressed ? 0.982 : activeHover ? 1.035 : selected && activeShell ? 1.018 : 1;
    cameraNormalRef.current.set(0, 0, 1).applyQuaternion(camera.quaternion);
    if (group.parent) {
      group.parent.getWorldQuaternion(parentQuaternionRef.current);
      cameraNormalRef.current.applyQuaternion(parentQuaternionRef.current.invert()).normalize();
    }
    pillTargetPositionRef.current.copy(cameraNormalRef.current).multiplyScalar(lift);
    const interpolation = reducedMotion ? 1 : 1 - Math.exp(-Math.min(0.08, delta) * 15);
    group.position.lerp(pillTargetPositionRef.current, interpolation);
    const nextScale = MathUtils.lerp(group.scale.x, scaleTarget, interpolation);
    group.scale.setScalar(nextScale);
  });
  const activateNode = () => {
    if (!activeShell) return;
    onSelect(node);
    if (isFolderLike(node)) onOpenFolder(node);
  };
  const scheduleNodeActivation = () => {
    if (clickTimerRef.current !== null) window.clearTimeout(clickTimerRef.current);
    clickTimerRef.current = window.setTimeout(() => {
      clickTimerRef.current = null;
      activateNode();
    }, 210);
  };
  const handleClick = (event: ThreeEvent<MouseEvent>) => {
    event.stopPropagation();
    if (!activeShell) return;
    scheduleNodeActivation();
  };
  const handleDoubleClick = (event: ThreeEvent<MouseEvent>) => {
    event.stopPropagation();
    if (!activeShell) return;
    if (clickTimerRef.current !== null) window.clearTimeout(clickTimerRef.current);
    clickTimerRef.current = null;
    onSelect(node);
    if (node.open_target && (node.kind === "file" || isFolderLike(node))) onOpenTarget(node);
  };
  const showPopup = activeShell && (hovered || selected);
  const roleLabel = role === "rootFolderNode"
    ? "Current root"
    : role === "folderNode"
      ? "Folder"
      : role === "fileLeafNode"
        ? "File"
        : "Indexed node";
  const labelOffset: Point3 = role === "rootFolderNode" ? [0, -1.38, 0] : [0, -(nodeRadius + 0.34), 0];

  return <group position={position} scale={inverseOrbScale}>
    <group ref={pillDepthGroupRef}>
      <mesh
        name={`telemetryNodeHitTarget-${role}`}
        onClick={handleClick}
        onDoubleClick={handleDoubleClick}
        raycast={activeShell ? undefined : () => undefined}
        onPointerOver={() => { if (activeShell) setHovered(true); }}
        onPointerOut={() => { setHovered(false); setPressed(false); }}
        onPointerDown={() => setPressed(true)}
        onPointerUp={() => setPressed(false)}
        userData={{
          nodeId: node.node_id,
          owningOrbId,
          depthSlot,
          interactiveAuthority: "ATTRIBUTABLE_NODE_IDENTITY_HIT_TARGET_AFTER_ROTATION",
          interactionLaw: role === "rootFolderNode"
            ? "SINGLE_CLICK_PRESERVES_ACCEPTED_ROOT_FOLDER_ACTION_DOUBLE_CLICK_OPENS_NATIVE_TARGET"
            : "SINGLE_CLICK_SELECTS_AND_FOLDER_SHIFTS_ROOT_DOUBLE_CLICK_OPENS_NATIVE_TARGET",
          activeShell,
          pillDepthLaw: "CAMERA_NORMAL_LIFT_SUBTLE_SCALE_RIM_REFRACTION_PARALLAX_SHADOW_NO_SPIN",
          sizeLaw: "ROOT_LARGEST_FOLDER_NODE_MEDIUM_FILE_EDGE_SMALLEST",
          visualScaleLaw: "ROOT_LARGEST_THEN_FOLDER_NODE_2_65X_FILE_VISUAL_HEIGHT",
          buttonRole: role === "rootFolderNode" ? "ROOT_NODE_BUTTON" : role === "folderNode" ? "FOLDER_NODE_BUTTON" : "FILE_EDGE_BUTTON",
          geometryAuthority: "LIGHTWEIGHT_TRANSPARENT_ALPHA_BUTTON_ASSET",
          runtimeRasterAsset: role === "rootFolderNode"
            ? T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.rootNode.sha256
            : role === "folderNode"
              ? T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.folderNode.sha256
              : T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.edgeNode.sha256,
          motionReduced: reducedMotion,
        }}
      >
        <sphereGeometry args={[nodeRadius, 24, 16]} />
        <meshBasicMaterial transparent opacity={0.001} depthWrite={false} colorWrite={false} />
      </mesh>
      <LightweightTelemetryButton
        node={node}
        role={role}
        owningOrbId={owningOrbId}
        tone={tone}
        brainColor={brainColor}
        selected={selected}
        contextOpacity={contextOpacity}
      />
      {activeShell && <Html center distanceFactor={13} position={labelOffset} zIndexRange={[28, 4]}>
      <span
        className={`telemetry3d-node-label telemetry3d-node-label--caption telemetry3d-node-label--interactive telemetry3d-node-label--${tone.toLowerCase()} telemetry3d-node-label--${role} ${showPopup ? "is-popup-visible" : ""}`}
        data-node-kind={node.kind}
        data-label-backplate="NONE"
        data-label-visibility="ACTIVE_SHELL_ONLY_WITH_EXACT_HOVER_POPUP"
        data-default-label-priority={defaultLabelVisible ? "PRIMARY" : "STANDARD"}
        title={node.label}
        aria-label={`${node.label}, ${roleLabel}`}
        onPointerEnter={() => setHovered(true)}
        onPointerLeave={() => { setHovered(false); setPressed(false); }}
      >
        <span className="telemetry3d-node-label__caption">{node.label}</span>
        {showPopup && <span className="telemetry3d-node-label__popup" role="tooltip">
          <strong>{node.label}</strong>
          <small>{roleLabel} · saved topology</small>
        </span>}
      </span>
      </Html>}
    </group>
  </group>;
}

function sceneShellRadius(layer: DataDerivedOrbLayer, outerRadius: number) {
  return outerRadius * layer.sceneShell.radius_ratio;
}

export function sceneCameraDistanceForFocus(
  baseDistance: number,
  focusedLayerIndex: number,
  outerRadius: number,
  terminalRootLayerIndex = Number.POSITIVE_INFINITY,
  focusedRadiusRatio = 1,
) {
  if (focusedLayerIndex <= 0) return baseDistance;
  if (focusedLayerIndex === terminalRootLayerIndex) return TELEMETRY_ROOT_BUTTON_RADIUS * 2.86;
  const focusedRadius = outerRadius * focusedRadiusRatio * 1.045;
  return Math.max(2.85, focusedRadius * 2.68);
}

function sceneShellIndex(orderedShellIds: string[], orbId: string | null | undefined) {
  if (!orbId) return 0;
  const index = orderedShellIds.indexOf(orbId);
  return index >= 0 ? index : 0;
}

function IndependentTelemetryOrb({
  layer,
  outerRadius,
  graph,
  overlayEnabled,
  selectedNodeId,
  focusRequest,
  orderedShellIds,
  focusedLayerIndex,
  focused,
  onFocusAudit,
  onRotationAudit,
  onHoveredOrbChange,
  rotationByOrbRef,
  onSelectNode,
  onOpenFolder,
  onOpenTarget,
  reducedMotion,
  brainColor,
}: {
  layer: DataDerivedOrbLayer;
  outerRadius: number;
  graph: TelemetryGraphResult;
  overlayEnabled: boolean;
  selectedNodeId: string;
  focusRequest: TelemetryOrbFocusRequest | null;
  orderedShellIds: string[];
  focusedLayerIndex: number;
  focused: boolean;
  onFocusAudit: (audit: TelemetryOrbFocusAudit) => void;
  onRotationAudit: (audit: TelemetryOrbRotationAudit) => void;
  onHoveredOrbChange: (orbId: string | null) => void;
  rotationByOrbRef: { current: Map<string, Point3> };
  onSelectNode: (node: TelemetryNode) => void;
  onOpenFolder: (node: TelemetryNode) => void;
  onOpenTarget: (node: TelemetryNode) => void;
  reducedMotion: boolean;
  brainColor: string;
}) {
  const { gl } = useThree();
  const [rotation, setRotation] = useState<Point3>(() => rotationByOrbRef.current.get(layer.orbId) || loadShellOrientation(layer.sceneShell));
  const rotationValueRef = useRef<Point3>(rotation);
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef<{
    pointerId: number;
    lastClientX: number;
    lastClientY: number;
    captureTarget: PointerCaptureTarget;
  } | null>(null);
  const lastFocusNonceRef = useRef(0);
  const tone = layerEvidenceTone(layer, graph, overlayEnabled);
  const nestedFocusScale = sceneShellRadius(layer, outerRadius);
  const outerWrapScale = outerRadius;
  const zDepthOffset = layer.layerIndex < focusedLayerIndex
    ? -(focusedLayerIndex - layer.layerIndex) * outerRadius * 0.22
    : 0;
  const inverseOrbScale = 1 / Math.max(0.0001, nestedFocusScale);
  const sceneDistance = Math.abs(layer.layerIndex - focusedLayerIndex);
  const contextOpacity = focused ? 1 : sceneDistance === 1 ? 0.38 : 0.16;
  const showMemberContext = focused || layer.layerIndex === focusedLayerIndex + 1;

  useEffect(() => {
    const request = focusRequest;
    if (!request || request.nextFocusedOrbId !== layer.orbId || request.nonce === lastFocusNonceRef.current) return;
    lastFocusNonceRef.current = request.nonce;
    const focusedLayerIndexBefore = sceneShellIndex(orderedShellIds, request.orbId);
    onFocusAudit({
      focusedOrbId: layer.orbId,
      direction: request.direction,
      focusedLayerIndexBefore,
      focusedLayerIndexAfter: layer.layerIndex,
      nestedFocusScale,
      outerWrapScale,
      moved: focusedLayerIndexBefore !== layer.layerIndex,
      hoveredAnchorPreserved: true,
      orientationPreserved: true,
      inactiveLayersRotationLocked: true,
      cameraSceneTravel: true,
      orbPopScaleMutation: 0,
      zDepthOffset,
      focusLaw: "CAMERA_SCENE_POINT_A_TO_POINT_B_NESTED_ORB_TRAVERSAL",
      sourceReadsDuringFocus: graph.raw_project_files_reread,
    });
  }, [focusRequest, graph.raw_project_files_reread, layer.layerIndex, layer.orbId, nestedFocusScale, onFocusAudit, orderedShellIds, outerWrapScale, zDepthOffset]);

  useEffect(() => {
    if (focused) return;
    const activeDrag = dragRef.current;
    if (activeDrag) activeDrag.captureTarget.releasePointerCapture?.(activeDrag.pointerId);
    dragRef.current = null;
    setDragging(false);
    if (gl.domElement.style.cursor === ORB_ACTIVE_CURSOR.cursor || gl.domElement.style.cursor === ORB_IDLE_CURSOR.cursor) {
      gl.domElement.style.cursor = "default";
    }
  }, [focused, gl]);

  useEffect(() => () => {
    if (gl.domElement.style.cursor === ORB_IDLE_CURSOR.cursor || gl.domElement.style.cursor === ORB_ACTIVE_CURSOR.cursor) {
      gl.domElement.style.cursor = "default";
    }
  }, [gl]);

  const setOrbCursor = (active: boolean) => {
    gl.domElement.style.cursor = focused
      ? active ? ORB_ACTIVE_CURSOR.cursor : ORB_IDLE_CURSOR.cursor
      : "zoom-in";
  };
  const handlePointerDown = (event: ThreeEvent<PointerEvent>) => {
    if (!focused || event.button !== 0) return;
    event.stopPropagation();
    const captureTarget = event.target as PointerCaptureTarget;
    captureTarget.setPointerCapture?.(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      lastClientX: event.nativeEvent.clientX,
      lastClientY: event.nativeEvent.clientY,
      captureTarget,
    };
    setDragging(true);
    setOrbCursor(true);
  };
  const handlePointerMove = (event: ThreeEvent<PointerEvent>) => {
    if (!focused) return;
    const drag = dragRef.current;
    if (!drag) return;
    if (drag.pointerId !== event.pointerId) return;
    event.stopPropagation();
    const dragDeltaX = event.nativeEvent.clientX - drag.lastClientX;
    const dragDeltaY = event.nativeEvent.clientY - drag.lastClientY;
    const dragDeltaZ = (dragDeltaX - dragDeltaY) * 0.0035;
    drag.lastClientX = event.nativeEvent.clientX;
    drag.lastClientY = event.nativeEvent.clientY;
    const rotationBefore = rotationValueRef.current;
    const rotationAfter: Point3 = [
      rotationBefore[0] + dragDeltaY * 0.007,
      rotationBefore[1] + dragDeltaX * 0.007,
      rotationBefore[2] + dragDeltaZ,
    ];
    rotationValueRef.current = rotationAfter;
    rotationByOrbRef.current.set(layer.orbId, rotationAfter);
    persistShellOrientation(layer.sceneShell, rotationAfter);
    setRotation(rotationAfter);
    onRotationAudit({
      orbId: layer.orbId,
      rotationBefore,
      rotationAfter,
      dragDeltaX,
      dragDeltaY,
      dragDeltaZ,
      otherLayersChanged: 0,
      sourceReadsDuringNavigation: graph.raw_project_files_reread,
    });
  };
  const releaseDrag = (event: ThreeEvent<PointerEvent>) => {
    if (!focused) return;
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    drag.captureTarget.releasePointerCapture?.(event.pointerId);
    dragRef.current = null;
    setDragging(false);
    setOrbCursor(false);
  };

  return <group
    name="independentTelemetryOrb"
    rotation={rotation}
    position={[0, 0, zDepthOffset]}
    scale={nestedFocusScale}
    onPointerOver={(event) => {
      event.stopPropagation();
      onHoveredOrbChange(layer.orbId);
      if (!dragging) setOrbCursor(false);
    }}
    onPointerOut={() => {
      if (!dragging) {
        onHoveredOrbChange(null);
        gl.domElement.style.cursor = "default";
      }
    }}
    onPointerDown={handlePointerDown}
    onPointerMove={handlePointerMove}
    onPointerUp={releaseDrag}
    onPointerCancel={releaseDrag}
    userData={{
      presentation: "DATA_DERIVED_OWNING_GLASS_ORB",
      rotationLaw: "ACTIVE_ORB_ONLY_FREE_XYZ_INACTIVE_LAYERS_LOCKED",
      rotationLocked: !focused,
      focusLaw: "CAMERA_SCENE_POINT_A_TO_POINT_B_NESTED_ORB_TRAVERSAL",
      zDepthLaw: "FIXED_CONCENTRIC_ORBS_CAMERA_TRAVELS_THROUGH_Z_DEPTH",
      orbId: layer.orbId,
      focusedLayerIndex,
      nestedFocusScale,
      savedRadiusRatio: layer.sceneShell.radius_ratio,
      outerWrapScale,
      zDepthOffset,
      focused,
      orientationPreserved: true,
      cameraSceneTravel: true,
      orbPopScaleMutation: 0,
      dragging,
    }}
  >
    <mesh name="independentOrbPointerHitTarget">
      <sphereGeometry args={[1.035, 28, 20]} />
      <meshBasicMaterial transparent opacity={0.001} depthWrite={false} colorWrite={false} />
    </mesh>
    <TopologyLayerLens layer={layer} tone={tone} brainColor={brainColor} active={focused} contextOpacity={contextOpacity} />
    {showMemberContext && layer.members.map((member) => <TelemetryNodeMesh
      key={member.node.node_id}
      node={member.node}
      role={member.role}
      position={member.unitPosition}
      owningOrbId={layer.orbId}
      inverseOrbScale={inverseOrbScale}
      defaultLabelVisible={member.defaultLabelVisible}
      activeShell={focused}
      contextOpacity={focused ? 1 : 0.3}
      overlayEnabled={overlayEnabled}
      selected={selectedNodeId === member.node.node_id}
      onSelect={onSelectNode}
      onOpenFolder={onOpenFolder}
      onOpenTarget={onOpenTarget}
      reducedMotion={reducedMotion}
      depthSlot={layer.layerIndex}
      brainColor={brainColor}
    />)}
  </group>;
}

function CameraRig({
  distance,
  resetNonce,
  focusedLayerIndex,
  terminalRootLayerIndex,
  outerRadius,
  focusedRadiusRatio,
  reducedMotion,
}: {
  distance: number;
  resetNonce: number;
  focusedLayerIndex: number;
  terminalRootLayerIndex: number;
  outerRadius: number;
  focusedRadiusRatio: number;
  reducedMotion: boolean;
}) {
  const { camera, gl } = useThree();
  const resetNonceRef = useRef(resetNonce);
  useEffect(() => {
    if (resetNonceRef.current === resetNonce) return;
    resetNonceRef.current = resetNonce;
    camera.position.set(0, 0, sceneCameraDistanceForFocus(distance, focusedLayerIndex, outerRadius, terminalRootLayerIndex, focusedRadiusRatio));
    camera.lookAt(0, 0, 0);
    camera.updateProjectionMatrix();
  }, [camera, distance, focusedLayerIndex, focusedRadiusRatio, outerRadius, resetNonce, terminalRootLayerIndex]);
  useFrame((_state, delta) => {
    const targetDistance = sceneCameraDistanceForFocus(distance, focusedLayerIndex, outerRadius, terminalRootLayerIndex, focusedRadiusRatio);
    const interpolation = reducedMotion ? 1 : 1 - Math.exp(-Math.min(0.08, delta) * 4.1);
    camera.position.x = MathUtils.lerp(camera.position.x, 0, interpolation);
    camera.position.y = MathUtils.lerp(camera.position.y, focusedLayerIndex > 0 ? outerRadius * 0.025 : 0, interpolation);
    camera.position.z = MathUtils.lerp(camera.position.z, targetDistance, interpolation);
    camera.lookAt(0, 0, 0);
    camera.updateProjectionMatrix();
    gl.domElement.dataset.sceneCameraZ = camera.position.z.toFixed(3);
    gl.domElement.dataset.sceneCameraTargetZ = targetDistance.toFixed(3);
    gl.domElement.dataset.sceneCameraFocusLayer = String(focusedLayerIndex);
    gl.domElement.dataset.sceneTerminalRootLayer = String(terminalRootLayerIndex);
    gl.domElement.dataset.sceneAtTerminalRoot = String(focusedLayerIndex === terminalRootLayerIndex);
    gl.domElement.dataset.sceneTravelMode = "POINT_A_TO_POINT_B_NO_ORB_POP";
    gl.domElement.dataset.sceneFocusedRadiusRatio = focusedRadiusRatio.toFixed(4);
  });
  return null;
}

function telemetryLayoutAudit(
  layout: TelemetryOrbLayout,
  graph: TelemetryGraphResult,
): TelemetryOrbLayoutAudit | null {
  const sceneIndex = layout.sceneIndex;
  if (!layout.root || !sceneIndex || layout.sceneIndexCacheStatus === "UNAVAILABLE") return null;
  const flattenedFileEdgeIds = sceneIndex.shells
    .filter((shell) => shell.shell_class === "FILE_EDGE_NODE_ORB")
    .flatMap((shell) => shell.node_ids);
  if (flattenedFileEdgeIds.length !== sceneIndex.file_edge_count) {
    throw new Error("TELEMETRY_SCENE_INDEX_NODE_LOSS_DETECTED");
  }
  if (new Set(flattenedFileEdgeIds).size !== flattenedFileEdgeIds.length) {
    throw new Error("TELEMETRY_SCENE_INDEX_NODE_DUPLICATION_DETECTED");
  }
  const terminalRootLayerIndex = sceneIndex.shells.findIndex(
    (shell) => shell.shell_id === sceneIndex.terminal_root_shell_id,
  );
  return {
    rootNodeId: layout.root.node_id,
    savedMembershipCount: sceneIndex.file_edge_count + sceneIndex.child_folder_count,
    orbCount: sceneIndex.total_glass_orb_count,
    distribution: sceneIndex.shells.map((shell) => shell.node_count),
    focusStopCount: sceneIndex.total_glass_orb_count,
    folderOrbCount: 1,
    edgeOrbCount: sceneIndex.file_edge_partition_distribution.length,
    edgeMembersPerOrb: TELEMETRY_FILE_EDGE_CAPACITY,
    fileEdgeNodeCount: sceneIndex.file_edge_count,
    childFolderNodeCount: sceneIndex.child_folder_count,
    fileEdgeDistribution: [...sceneIndex.file_edge_partition_distribution],
    sceneDistribution: sceneIndex.shells.map((shell) => shell.node_count),
    sceneTransitionCount: sceneIndex.scene_transition_count,
    orderedShellIds: [...sceneIndex.ordered_shell_ids],
    sceneIndexCacheKey: sceneIndex.cache_key,
    sceneIndexCacheStatus: layout.sceneIndexCacheStatus,
    topologySnapshotHash: sceneIndex.topology_snapshot_hash,
    canonicalFileEdgeNodeIds: [...sceneIndex.canonical_file_edge_node_ids],
    canonicalChildFolderNodeIds: [...sceneIndex.canonical_child_folder_node_ids],
    maxFileEdgeShellPopulation: Math.max(0, ...sceneIndex.file_edge_partition_distribution),
    nodeLossCount: 0,
    nodeDuplicateCount: 0,
    terminalRootOrbId: sceneIndex.terminal_root_shell_id,
    terminalRootLayerIndex,
    terminalRootNonRotating: true,
    sourceReadsDuringLayout: graph.raw_project_files_reread,
  };
}

function TelemetrySceneContents(props: BrainTelemetrySceneProps) {
  const { size } = useThree();
  const rotationByOrbRef = useRef(new Map<string, Point3>());
  const lastRootFocusNonceRef = useRef(0);
  const outerRadius = useMemo(() => {
    const visibleHalfHeight = Math.tan((TELEMETRY_LAYOUT_FOV * Math.PI / 180) / 2) * TELEMETRY_LAYOUT_DISTANCE;
    const aspect = Math.max(0.75, size.width / Math.max(1, size.height));
    return Math.min(visibleHalfHeight * 0.9, visibleHalfHeight * aspect * 0.9);
  }, [size.height, size.width]);
  const layout = useMemo(
    () => buildDataDerivedOrbLayers(props.graph, outerRadius, props.brainColor),
    [outerRadius, props.brainColor, props.graph],
  );
  const orderedShellIds = layout.sceneIndex?.ordered_shell_ids || [];
  const terminalRootOrbId = layout.sceneIndex?.terminal_root_shell_id || "";
  const terminalRootLayerIndex = Math.max(0, sceneShellIndex(orderedShellIds, terminalRootOrbId));
  const focusedLayerIndex = Math.min(
    terminalRootLayerIndex,
    sceneShellIndex(orderedShellIds, props.focusedOrbId),
  );
  const focusedRadiusRatio = layout.sceneIndex?.shells[focusedLayerIndex]?.radius_ratio || 1;
  useEffect(() => {
    const request = props.focusRequest;
    if (!request || request.nextFocusedOrbId !== terminalRootOrbId || request.nonce === lastRootFocusNonceRef.current) return;
    lastRootFocusNonceRef.current = request.nonce;
    props.onFocusAudit({
      focusedOrbId: terminalRootOrbId,
      direction: request.direction,
      focusedLayerIndexBefore: sceneShellIndex(orderedShellIds, request.orbId),
      focusedLayerIndexAfter: terminalRootLayerIndex,
      nestedFocusScale: TELEMETRY_ROOT_BUTTON_RADIUS,
      outerWrapScale: layout.outerRadius,
      moved: sceneShellIndex(orderedShellIds, request.orbId) !== terminalRootLayerIndex,
      hoveredAnchorPreserved: true,
      orientationPreserved: true,
      inactiveLayersRotationLocked: true,
      cameraSceneTravel: true,
      orbPopScaleMutation: 0,
      zDepthOffset: 0,
      focusLaw: "CAMERA_SCENE_POINT_A_TO_POINT_B_NESTED_ORB_TRAVERSAL",
      sourceReadsDuringFocus: props.graph.raw_project_files_reread,
    });
  }, [layout.outerRadius, orderedShellIds, props.focusRequest, props.graph.raw_project_files_reread, props.onFocusAudit, terminalRootLayerIndex, terminalRootOrbId]);
  useEffect(() => {
    const audit = telemetryLayoutAudit(layout, props.graph);
    if (audit) props.onLayoutAudit(audit);
  }, [layout, props.graph, props.onLayoutAudit]);
  const rootTone = layout.root
    ? effectiveTone(layout.root.tone, props.overlayEnabled, layout.root.overlay_evidence_direct)
    : "NEUTRAL";
  const fixedOuterBackDepth = focusedLayerIndex > 0
    ? -focusedLayerIndex * layout.outerRadius * 0.22
    : 0;

  return <>
    <CameraRig
      distance={props.cameraDistance}
      resetNonce={props.cameraResetNonce}
      focusedLayerIndex={focusedLayerIndex}
      terminalRootLayerIndex={terminalRootLayerIndex}
      outerRadius={layout.outerRadius}
      focusedRadiusRatio={focusedRadiusRatio}
      reducedMotion={props.reducedMotion}
    />
    <ambientLight intensity={0.92} />
    <directionalLight position={[4, 7, 9]} intensity={1.45} color="#eafaff" />
    <pointLight position={[-7, -2, 5]} intensity={38} distance={28} color="#74d8ff" />
    <pointLight position={[7, 4, 3]} intensity={18} distance={24} color="#e8fbff" />
    <group position={[0, 0, fixedOuterBackDepth]} userData={{ backLayerPresence: true, rotationLocked: true }}>
      <FixedOuterNeonRim radius={layout.outerRadius * 1.045} color={props.brainColor || GOLD} />
    </group>
    <group
      name="dynamicIndependentSavedMembershipOrbs"
      userData={{
        layoutLaw: "FIXED_OUTER_FOLDER_ORB_EDGE_ORBS_MAX_20_TERMINAL_ROOT_BUTTON",
        savedMembershipCount: (layout.sceneIndex?.file_edge_count || 0) + (layout.sceneIndex?.child_folder_count || 0),
        totalGlassOrbCount: layout.sceneIndex?.total_glass_orb_count || 0,
        cameraFocusStopCount: layout.sceneIndex?.total_glass_orb_count || 0,
        sceneTransitionCount: layout.sceneIndex?.scene_transition_count || 0,
        sceneIndexCacheKey: layout.sceneIndex?.cache_key || "",
        sceneIndexCacheStatus: layout.sceneIndexCacheStatus,
        fileEdgeDistribution: layout.sceneIndex?.file_edge_partition_distribution || [],
        orderedShellIds,
        folderOrbCount: 1,
        edgeOrbCount: layout.layers.filter((layer) => layer.semanticRole === "EDGE_NODE_ORB").length,
        edgeMembersPerOrb: TELEMETRY_FILE_EDGE_CAPACITY,
        terminalRootOrbId,
        terminalRootLayerIndex,
        terminalRootRotationLocked: true,
        fixedLayerCount: false,
        semanticRelationGeometry: "NONE_USER_DEFERRED",
        topologyConnectorLineCount: 0,
        nodeGeometryAuthority: "LIGHTWEIGHT_TRANSPARENT_ALPHA_BUTTONS_INSIDE_PROCEDURAL_3D_GLASS",
        rotationLaw: "ACTIVE_ORB_ONLY_FREE_XYZ_INACTIVE_LAYERS_LOCKED",
        zDepthLaw: "FIXED_CONCENTRIC_ORBS_CAMERA_SCENE_TRAVELS_POINT_A_TO_POINT_B",
        focusTravelLaw: "CAMERA_INTERPOLATION_NO_ORB_POP_SCALE_MUTATION",
        outerNeonLaw: "SELECTED_BRAIN_COLOR_FIXED_2D_OUTER_RIM_ONLY_GLASS_VOLUME_TRANSPARENT",
        sourceReadsDuringLayout: props.graph.raw_project_files_reread,
        sourceReadsDuringFocus: props.graph.raw_project_files_reread,
        canonicalProjectTruthMutated: false,
      }}
    >
      {layout.layers.map((layer) => <IndependentTelemetryOrb
        key={layer.orbId}
        layer={layer}
        outerRadius={layout.outerRadius}
        graph={props.graph}
        overlayEnabled={props.overlayEnabled}
        selectedNodeId={props.selectedNodeId}
        focusRequest={props.focusRequest}
        orderedShellIds={orderedShellIds}
        focusedLayerIndex={focusedLayerIndex}
        focused={layer.layerIndex === focusedLayerIndex}
        onFocusAudit={props.onFocusAudit}
        onRotationAudit={props.onRotationAudit}
        onHoveredOrbChange={props.onHoveredOrbChange}
        rotationByOrbRef={rotationByOrbRef}
        onSelectNode={props.onSelectNode}
        onOpenFolder={props.onOpenFolder}
        onOpenTarget={props.onOpenTarget}
        reducedMotion={props.reducedMotion}
        brainColor={props.brainColor}
      />)}
      {layout.root && <TelemetryNodeMesh
        node={layout.root}
        role="rootFolderNode"
        position={[0, 0, 0]}
        owningOrbId={terminalRootOrbId}
        inverseOrbScale={1}
        defaultLabelVisible
        activeShell={focusedLayerIndex === terminalRootLayerIndex}
        contextOpacity={focusedLayerIndex === terminalRootLayerIndex ? 1 : 0.44}
        overlayEnabled={props.overlayEnabled}
        selected={props.selectedNodeId === layout.root.node_id}
        onSelect={props.onSelectNode}
        onOpenFolder={props.onOpenFolder}
        onOpenTarget={props.onOpenTarget}
        reducedMotion={props.reducedMotion}
        depthSlot={terminalRootLayerIndex}
        brainColor={props.brainColor}
      />}
    </group>
    <group visible={false} userData={{ selectedEdgeId: props.selectedEdgeId, onSelectEdgeAuthority: Boolean(props.onSelectEdge) }} />
  </>;
}

function TelemetryDomNodeButton({
  node,
  role,
  owningOrbId,
  selected,
  activeShell,
  contextOpacity,
  style,
  onSelectNode,
  onOpenFolder,
  onOpenTarget,
}: {
  node: TelemetryNode;
  role: TelemetryVisualRole;
  owningOrbId: string;
  selected: boolean;
  activeShell: boolean;
  contextOpacity: number;
  style?: CSSProperties;
  onSelectNode: (node: TelemetryNode) => void;
  onOpenFolder: (node: TelemetryNode) => void;
  onOpenTarget: (node: TelemetryNode) => void;
}) {
  const clickTimerRef = useRef<number | null>(null);
  useEffect(() => () => {
    if (clickTimerRef.current !== null) window.clearTimeout(clickTimerRef.current);
  }, []);
  const roleLabel = role === "rootFolderNode" ? "Current root" : role === "folderNode" ? "Folder" : "File";
  const scheduleNodeActivation = () => {
    if (clickTimerRef.current !== null) window.clearTimeout(clickTimerRef.current);
    clickTimerRef.current = window.setTimeout(() => {
      clickTimerRef.current = null;
      onSelectNode(node);
      if (isFolderLike(node)) onOpenFolder(node);
    }, 210);
  };
  const handleDoubleClick = () => {
    if (clickTimerRef.current !== null) window.clearTimeout(clickTimerRef.current);
    clickTimerRef.current = null;
    onSelectNode(node);
    if (node.open_target && (node.kind === "file" || isFolderLike(node))) onOpenTarget(node);
  };
  const isRoot = role === "rootFolderNode";
  const buttonStyle = {
    ...style,
    "--pill-context-opacity": contextOpacity,
  } as CSSProperties;
  return <button
    type="button"
    className={`${isRoot ? "telemetry3d-dom-root" : `telemetry3d-dom-node telemetry3d-dom-node--${role}`} ${selected ? "is-selected" : ""} ${activeShell ? "is-active-shell" : "is-context-shell"}`}
    style={buttonStyle}
    data-node-id={node.node_id}
    data-node-kind={node.kind}
    data-owning-orb-id={owningOrbId}
    data-label-visibility="ACTIVE_SHELL_ONLY_WITH_EXACT_HOVER_POPUP"
    data-label-backplate="NONE"
    data-button-role={isRoot ? "ROOT_NODE_BUTTON" : role === "folderNode" ? "FOLDER_NODE_BUTTON" : "FILE_EDGE_BUTTON"}
    data-root-terminal-button={isRoot ? "true" : "false"}
    data-rotation-locked={isRoot ? "true" : undefined}
    data-single-click-law={isRoot ? "PRESERVE_ACCEPTED_ROOT_FOLDER_ACTION" : "SELECT_AND_FOLDER_SHIFTS_ROOT"}
    data-active-shell={activeShell ? "true" : "false"}
    data-pill-depth-law="CAMERA_NORMAL_LIFT_SUBTLE_SCALE_RIM_REFRACTION_PARALLAX_SHADOW_NO_SPIN"
    data-geometry-authority="LIGHTWEIGHT_TRANSPARENT_ALPHA_BUTTON_ASSET"
    data-runtime-raster-asset={isRoot
      ? T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.rootNode.sha256
      : role === "folderNode"
        ? T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.folderNode.sha256
        : T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.edgeNode.sha256}
    aria-label={`${node.label}, ${roleLabel}`}
    onPointerDown={(event) => event.stopPropagation()}
    aria-disabled={!activeShell}
    onClick={(event) => { event.stopPropagation(); if (activeShell) scheduleNodeActivation(); }}
    onDoubleClick={(event) => { event.stopPropagation(); if (activeShell) handleDoubleClick(); }}
  >
    <span className={`telemetry3d-dom-glass-button telemetry3d-dom-glass-button--${role}`} aria-hidden="true">
      <span className="telemetry3d-dom-glass-button__inner-volume" />
      <span className="telemetry3d-dom-glass-button__corneal-glare" />
      <img
        className={`telemetry3d-dom-glass-button__asset telemetry3d-dom-glass-button__asset--${role}`}
        src={isRoot
          ? T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.rootNode.url
          : role === "folderNode"
            ? T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.folderNode.url
            : T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.edgeNode.url}
        alt=""
        draggable={false}
      />
    </span>
    {activeShell && <span className={isRoot ? "telemetry3d-dom-root__label" : "telemetry3d-dom-node__label"}>{node.label}</span>}
    {activeShell && <span className={isRoot ? "telemetry3d-dom-root__popup" : "telemetry3d-dom-node__popup"} role="tooltip">
      <strong>{node.label}</strong>
      <small>{roleLabel} / saved topology</small>
    </span>}
  </button>;
}

function TelemetryFallbackOrb({
  layer,
  orbCount,
  graph,
  overlayEnabled,
  selectedNodeId,
  focusRequest,
  orderedShellIds,
  focusedLayerIndex,
  focused,
  onFocusAudit,
  onRotationAudit,
  onHoveredOrbChange,
  reducedMotion,
  onSelectNode,
  onOpenFolder,
  onOpenTarget,
}: {
  layer: DataDerivedOrbLayer;
  orbCount: number;
  graph: TelemetryGraphResult;
  overlayEnabled: boolean;
  selectedNodeId: string;
  focusRequest: TelemetryOrbFocusRequest | null;
  orderedShellIds: string[];
  focusedLayerIndex: number;
  focused: boolean;
  onFocusAudit: (audit: TelemetryOrbFocusAudit) => void;
  onRotationAudit: (audit: TelemetryOrbRotationAudit) => void;
  onHoveredOrbChange: (orbId: string | null) => void;
  reducedMotion: boolean;
  onSelectNode: (node: TelemetryNode) => void;
  onOpenFolder: (node: TelemetryNode) => void;
  onOpenTarget: (node: TelemetryNode) => void;
}) {
  const [rotation, setRotation] = useState<Point3>(() => loadShellOrientation(layer.sceneShell));
  const rotationRef = useRef<Point3>(rotation);
  const lastFocusNonceRef = useRef(0);
  const dragRef = useRef<{
    pointerId: number;
    lastClientX: number;
    lastClientY: number;
    target: HTMLDivElement;
  } | null>(null);
  const tone = layerEvidenceTone(layer, graph, overlayEnabled);
  const nestedFocusScale = layer.sceneShell.radius_ratio;
  const outerWrapScale = 1;
  const zDepthOffset = layer.layerIndex < focusedLayerIndex
    ? -(focusedLayerIndex - layer.layerIndex) * 1.05
    : 0;
  const sceneDistance = Math.abs(layer.layerIndex - focusedLayerIndex);
  const contextOpacity = focused ? 1 : sceneDistance === 1 ? 0.38 : 0.16;
  const showMemberContext = focused || layer.layerIndex === focusedLayerIndex + 1;

  useEffect(() => {
    const request = focusRequest;
    if (!request || request.nextFocusedOrbId !== layer.orbId || request.nonce === lastFocusNonceRef.current) return;
    lastFocusNonceRef.current = request.nonce;
    const focusedLayerIndexBefore = sceneShellIndex(orderedShellIds, request.orbId);
    onFocusAudit({
      focusedOrbId: layer.orbId,
      direction: request.direction,
      focusedLayerIndexBefore,
      focusedLayerIndexAfter: layer.layerIndex,
      nestedFocusScale,
      outerWrapScale,
      moved: focusedLayerIndexBefore !== layer.layerIndex,
      hoveredAnchorPreserved: true,
      orientationPreserved: true,
      inactiveLayersRotationLocked: true,
      cameraSceneTravel: true,
      orbPopScaleMutation: 0,
      zDepthOffset,
      focusLaw: "CAMERA_SCENE_POINT_A_TO_POINT_B_NESTED_ORB_TRAVERSAL",
      sourceReadsDuringFocus: graph.raw_project_files_reread,
    });
  }, [focusRequest, graph.raw_project_files_reread, layer.layerIndex, layer.orbId, nestedFocusScale, onFocusAudit, orderedShellIds, outerWrapScale, zDepthOffset]);

  const releaseDrag = (pointerId: number) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== pointerId) return;
    if (drag.target.hasPointerCapture(pointerId)) drag.target.releasePointerCapture(pointerId);
    dragRef.current = null;
  };
  useEffect(() => {
    if (focused || !dragRef.current) return;
    const drag = dragRef.current;
    if (drag.target.hasPointerCapture(drag.pointerId)) drag.target.releasePointerCapture(drag.pointerId);
    dragRef.current = null;
  }, [focused]);
  const orbStyle = {
    "--fallback-orb-scale": nestedFocusScale,
    "--fallback-orb-z": `${zDepthOffset * 92}px`,
    "--fallback-orb-rotation-x": `${rotation[0]}rad`,
    "--fallback-orb-rotation-y": `${rotation[1]}rad`,
    "--fallback-orb-rotation-z": `${rotation[2]}rad`,
    "--fallback-orb-order": orbCount - layer.layerIndex,
    "--fallback-shell-context-opacity": contextOpacity,
  } as CSSProperties;

  return <div
    className={`telemetry3d-dom-orb telemetry3d-dom-orb--${tone.toLowerCase()} ${reducedMotion ? "is-motion-reduced" : ""}`}
    style={orbStyle}
    data-orb-id={layer.orbId}
    data-home-depth-slot={layer.homeDepthSlot}
    data-semantic-orb-role={layer.semanticRole}
    data-edge-members-per-orb={layer.semanticRole === "EDGE_NODE_ORB" ? TELEMETRY_FILE_EDGE_CAPACITY : 0}
    data-outermost={layer.layerIndex === 0 ? "true" : "false"}
    data-focused={focused ? "true" : "false"}
    data-focus-scale={nestedFocusScale}
    data-outer-wrap-scale={outerWrapScale}
    data-z-depth-offset={zDepthOffset}
    data-rotation-locked={focused ? "false" : "true"}
    data-rotation-law="ACTIVE_ORB_ONLY_FREE_XYZ_INACTIVE_LAYERS_LOCKED"
    data-geometry-law={layer.geometryLaw}
    data-focus-law="CAMERA_SCENE_POINT_A_TO_POINT_B_NO_ORB_POP"
    data-connector-line-count={layer.connectorLineCount}
    data-shell-thickness-law="HUMAN_EYE_LENS_OUTER_CORNEA_INNER_VOLUME_REFRACTIVE_THICKNESS"
    onPointerEnter={() => onHoveredOrbChange(layer.orbId)}
    onPointerLeave={(event) => {
      releaseDrag(event.pointerId);
      onHoveredOrbChange(null);
    }}
    onPointerDown={(event) => {
      if (!focused || event.button !== 0) return;
      event.stopPropagation();
      event.currentTarget.setPointerCapture(event.pointerId);
      dragRef.current = {
        pointerId: event.pointerId,
        lastClientX: event.clientX,
        lastClientY: event.clientY,
        target: event.currentTarget,
      };
    }}
    onPointerMove={(event) => {
      if (!focused) return;
      const drag = dragRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      event.stopPropagation();
      const dragDeltaX = event.clientX - drag.lastClientX;
      const dragDeltaY = event.clientY - drag.lastClientY;
      const dragDeltaZ = (dragDeltaX - dragDeltaY) * 0.0035;
      drag.lastClientX = event.clientX;
      drag.lastClientY = event.clientY;
      const rotationBefore = rotationRef.current;
      const rotationAfter: Point3 = [
        rotationBefore[0] + dragDeltaY * 0.007,
        rotationBefore[1] + dragDeltaX * 0.007,
        rotationBefore[2] + dragDeltaZ,
      ];
      rotationRef.current = rotationAfter;
      persistShellOrientation(layer.sceneShell, rotationAfter);
      setRotation(rotationAfter);
      onRotationAudit({
        orbId: layer.orbId,
        rotationBefore,
        rotationAfter,
        dragDeltaX,
        dragDeltaY,
        dragDeltaZ,
        otherLayersChanged: 0,
        sourceReadsDuringNavigation: graph.raw_project_files_reread,
      });
    }}
    onPointerUp={(event) => releaseDrag(event.pointerId)}
    onPointerCancel={(event) => releaseDrag(event.pointerId)}
    role="group"
    aria-label={`Saved membership glass orb ${layer.layerIndex + 1}, ${layer.members.length} nodes`}
  >
    {layer.layerIndex === 0 && <img
      className="telemetry3d-dom-orb__universal-asset"
      src={T023_LIGHTWEIGHT_TELEMETRY_SCENE_ASSETS_V1.assets.universalGlassOrb.url}
      alt=""
      draggable={false}
      aria-hidden="true"
    />}
    <span className="telemetry3d-dom-orb__corneal-glare" aria-hidden="true" />
    {showMemberContext && layer.members.map((member) => {
      const [x, y, z] = member.unitPosition;
      const memberTone = effectiveTone(member.node.tone, overlayEnabled, member.node.overlay_evidence_direct);
      const color = toneColor(memberTone, member.role === "folderNode" ? "#64d7f3" : "#b8eafb");
      const nodeStyle = {
        "--fallback-node-x": `${50 + x * 40}%`,
        "--fallback-node-y": `${50 - y * 40}%`,
        "--fallback-node-z": `${z * 42}px`,
        "--fallback-node-color": color,
      } as CSSProperties;
      return <TelemetryDomNodeButton
        key={member.node.node_id}
        node={member.node}
        role={member.role}
        owningOrbId={layer.orbId}
        selected={selectedNodeId === member.node.node_id}
        activeShell={focused}
        contextOpacity={focused ? 1 : 0.3}
        style={nodeStyle}
        onSelectNode={onSelectNode}
        onOpenFolder={onOpenFolder}
        onOpenTarget={onOpenTarget}
      />;
    })}
  </div>;
}

function TelemetryDomFallback(props: BrainTelemetrySceneProps) {
  const layout = useMemo(
    () => buildDataDerivedOrbLayers(props.graph, 1, props.brainColor),
    [props.brainColor, props.graph],
  );
  const lastRootFocusNonceRef = useRef(0);
  const orderedShellIds = layout.sceneIndex?.ordered_shell_ids || [];
  const terminalRootOrbId = layout.sceneIndex?.terminal_root_shell_id || "";
  const terminalRootLayerIndex = Math.max(0, sceneShellIndex(orderedShellIds, terminalRootOrbId));
  const focusedLayerIndex = Math.min(
    terminalRootLayerIndex,
    sceneShellIndex(orderedShellIds, props.focusedOrbId),
  );
  const focusedRadiusRatio = layout.sceneIndex?.shells[focusedLayerIndex]?.radius_ratio || 1;
  const sceneTravelZ = focusedLayerIndex <= 0 ? 0 : Math.round((1 - focusedRadiusRatio) * 1120);
  useEffect(() => {
    const request = props.focusRequest;
    if (!request || request.nextFocusedOrbId !== terminalRootOrbId || request.nonce === lastRootFocusNonceRef.current) return;
    lastRootFocusNonceRef.current = request.nonce;
    props.onFocusAudit({
      focusedOrbId: terminalRootOrbId,
      direction: request.direction,
      focusedLayerIndexBefore: sceneShellIndex(orderedShellIds, request.orbId),
      focusedLayerIndexAfter: terminalRootLayerIndex,
      nestedFocusScale: TELEMETRY_ROOT_BUTTON_RADIUS,
      outerWrapScale: 1,
      moved: sceneShellIndex(orderedShellIds, request.orbId) !== terminalRootLayerIndex,
      hoveredAnchorPreserved: true,
      orientationPreserved: true,
      inactiveLayersRotationLocked: true,
      cameraSceneTravel: true,
      orbPopScaleMutation: 0,
      zDepthOffset: 0,
      focusLaw: "CAMERA_SCENE_POINT_A_TO_POINT_B_NESTED_ORB_TRAVERSAL",
      sourceReadsDuringFocus: props.graph.raw_project_files_reread,
    });
  }, [orderedShellIds, props.focusRequest, props.graph.raw_project_files_reread, props.onFocusAudit, terminalRootLayerIndex, terminalRootOrbId]);
  useEffect(() => {
    const audit = telemetryLayoutAudit(layout, props.graph);
    if (audit) props.onLayoutAudit(audit);
  }, [layout, props.graph, props.onLayoutAudit]);
  return <div
    className={`telemetry3d-dom-fallback ${props.reducedMotion ? "is-motion-reduced" : ""}`}
    data-renderer-authority="SAVED_TOPOLOGY_DOM_FALLBACK"
    data-webgl-authority="UNAVAILABLE_FALLBACK_ACTIVE"
    data-data-derived-orb-count={layout.sceneIndex?.total_glass_orb_count || 0}
    data-data-derived-orb-distribution={layout.sceneIndex?.shells.map((shell) => shell.node_count).join(",") || ""}
    data-file-edge-orb-distribution={layout.sceneIndex?.file_edge_partition_distribution.join(",") || ""}
    data-scene-focus-stop-count={layout.sceneIndex?.total_glass_orb_count || 0}
    data-scene-transition-count={layout.sceneIndex?.scene_transition_count || 0}
    data-folder-orb-count="1"
    data-edge-orb-count={layout.layers.filter((layer) => layer.semanticRole === "EDGE_NODE_ORB").length}
    data-edge-members-per-orb={TELEMETRY_FILE_EDGE_CAPACITY}
    data-scene-index-cache-key={layout.sceneIndex?.cache_key || ""}
    data-scene-index-cache-status={layout.sceneIndexCacheStatus}
    data-terminal-root-orb-id={terminalRootOrbId}
    data-terminal-root-non-rotating="true"
    data-raw-project-files-reread={props.graph.raw_project_files_reread}
    data-connector-law="NONE_USER_DEFERRED"
    data-connector-line-count="0"
    data-node-geometry-law="LIGHTWEIGHT_TRANSPARENT_ALPHA_BUTTONS_INSIDE_PROCEDURAL_3D_GLASS"
    data-lens-law="HUMAN_EYE_LENS_OUTER_CORNEA_INNER_VOLUME_REFRACTIVE_THICKNESS"
    data-focus-travel-law="CAMERA_SCENE_POINT_A_TO_POINT_B_NO_ORB_POP"
    data-outer-neon-law="SELECTED_BRAIN_COLOR_FIXED_2D_OUTER_RIM_ONLY_GLASS_VOLUME_TRANSPARENT"
    data-pulse-law="DEFERRED_TO_SEPARATE_ANIMATION_TASK"
    role="application"
    aria-label="Saved topology glass orb renderer"
    onClick={props.onSelectScene}
  >
    <div
      className="telemetry3d-dom-fallback__stage"
      style={{ "--fallback-scene-travel-z": `${sceneTravelZ}px` } as CSSProperties}
      data-focused-layer-index={focusedLayerIndex}
    >
      {layout.layers.map((layer) => <TelemetryFallbackOrb
        key={layer.orbId}
        layer={layer}
        orbCount={layout.sceneIndex?.total_glass_orb_count || 0}
        graph={props.graph}
        overlayEnabled={props.overlayEnabled}
        selectedNodeId={props.selectedNodeId}
        focusRequest={props.focusRequest}
        orderedShellIds={orderedShellIds}
        focusedLayerIndex={focusedLayerIndex}
        focused={layer.layerIndex === focusedLayerIndex}
        onFocusAudit={props.onFocusAudit}
        onRotationAudit={props.onRotationAudit}
        onHoveredOrbChange={props.onHoveredOrbChange}
        reducedMotion={props.reducedMotion}
        onSelectNode={props.onSelectNode}
        onOpenFolder={props.onOpenFolder}
        onOpenTarget={props.onOpenTarget}
      />)}
      {layout.root && <TelemetryDomNodeButton
        node={layout.root}
        role="rootFolderNode"
        owningOrbId={terminalRootOrbId}
        selected={props.selectedNodeId === layout.root.node_id}
        activeShell={focusedLayerIndex === terminalRootLayerIndex}
        contextOpacity={focusedLayerIndex === terminalRootLayerIndex ? 1 : 0.44}
        onSelectNode={props.onSelectNode}
        onOpenFolder={props.onOpenFolder}
        onOpenTarget={props.onOpenTarget}
      />}
    </div>
    <span className="telemetry3d-dom-fallback__badge">SAVED TOPOLOGY / GPU IDLE FALLBACK</span>
  </div>;
}

class TelemetrySceneBoundary extends Component<{
  children: ReactNode;
  fallback: ReactNode;
  resetKey: string;
}, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(_error: Error, _info: ErrorInfo) { /* saved-topology fallback rendered below */ }
  componentDidUpdate(previousProps: Readonly<{ children: ReactNode; fallback: ReactNode; resetKey: string }>) {
    if (this.state.failed && previousProps.resetKey !== this.props.resetKey) this.setState({ failed: false });
  }
  render() {
    if (this.state.failed) return this.props.fallback;
    return this.props.children;
  }
}

export function BrainTelemetryScene(props: BrainTelemetrySceneProps) {
  const resetKey = `${props.graph.scope.scope_id || "root"}:${props.graph.nodes.map((node) => node.node_id).join("|")}`;
  return <TelemetrySceneBoundary resetKey={resetKey} fallback={<TelemetryDomFallback {...props} />}>
    <Canvas
      className="telemetry3d-canvas"
      camera={{ position: [0, 0, props.cameraDistance], fov: TELEMETRY_LAYOUT_FOV, near: 0.1, far: 120 }}
      dpr={[1, 1.6]}
      gl={{ alpha: true, antialias: true, powerPreference: "high-performance" }}
      onPointerMissed={() => {
        props.onHoveredOrbChange(null);
        props.onSelectScene();
      }}
      onPointerLeave={() => props.onHoveredOrbChange(null)}
    >
      <TelemetrySceneContents {...props} />
    </Canvas>
  </TelemetrySceneBoundary>;
}
