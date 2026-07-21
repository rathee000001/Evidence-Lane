import { useEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent, type ReactNode, type WheelEvent } from "react";
import type {
  TelemetryContractResult,
  TelemetryDelta,
  TelemetryExecute,
  TelemetryGraphResult,
  TelemetryNode,
  PersistedTelemetryBundle,
  TelemetryRelationEdge,
  TelemetrySnapshotResult,
} from "../simulation/BrainTelemetryContract";
import type { SelectedBrainAuthorityMode } from "../contracts/T023UniversalUiSystemSchemas";
import {
  telemetryErrorCode,
  telemetryNodePath,
  telemetryResult,
} from "../simulation/BrainTelemetryContract";
import { resolveTelemetrySceneStep } from "../simulation/TelemetrySceneIndex";
import { BrainGlassSphere } from "./BrainGlassSphere";
import { BrainTelemetryScene, buildTelemetryNeighborhood } from "./BrainTelemetryScene";
import type {
  TelemetryOrbFocusAudit,
  TelemetryOrbFocusRequest,
  TelemetryOrbLayoutAudit,
  TelemetryOrbRotationAudit,
} from "./BrainTelemetryScene";
import { CollapsedSidePanelShell } from "./CollapsedSidePanelShell";
import { EvidenceHeaderShell } from "./EvidenceHeaderShell";
import { ExpandedSidePanelShell } from "./ExpandedSidePanelShell";
import { GlassIconOrb } from "./GlassIconOrb";
import { GlassPill } from "./GlassPill";
import { GlassShell } from "./GlassShell";
import { WorkspaceShell } from "./WorkspaceShell";

type TelemetrySelection = "none" | "scene" | "node" | "edge";

export type BrainTelemetry3DPageProps = {
  execute: TelemetryExecute;
  telemetryBundle: PersistedTelemetryBundle | null;
  brains: string[];
  selectedBrain: string;
  activeBrain: string;
  selectionAuthorityMode: SelectedBrainAuthorityMode;
  brainColors: Record<string, string>;
  onSelectBrain: (name: string) => Promise<void>;
  onJumpSQLite: () => void;
  onEscapeAdmin: () => void;
  railCollapsed: boolean;
  onRailCollapsedChange: (collapsed: boolean) => void;
  onRailAction?: (action: "add" | "search" | "pin") => void;
  profileName: string;
  profileAvatar?: ReactNode;
  onOpenProfile: () => void;
  reducedMotion: boolean;
  fullViewPromptBar?: ReactNode;
};

const CAMERA_DEFAULT_DISTANCE = 19;
const CODE_FILE_EXTENSIONS = new Set([
  ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
  ".java", ".js", ".json", ".jsx", ".kt", ".md", ".php", ".ps1", ".py",
  ".rb", ".rs", ".scss", ".sh", ".sql", ".swift", ".toml", ".ts", ".tsx",
  ".vue", ".xml", ".yaml", ".yml",
]);

type CachedTelemetryState = {
  contract: TelemetryContractResult;
  snapshot: TelemetrySnapshotResult;
  deltas: TelemetryDelta[];
  deltaVersionCount: number;
  graphs: Record<string, TelemetryGraphResult>;
  durableLookupMs: number;
};

const BASE_GRAPH_CACHE_KEY = "__ACCEPTED_SNAPSHOT__";

export function resolveNestedOrbFocus(
  orderedOrbIds: string[],
  focusedOrbId: string | null,
  hoveredOrbId: string | null,
  direction: "INWARD" | "OUTWARD",
) {
  return resolveTelemetrySceneStep(orderedOrbIds, focusedOrbId, hoveredOrbId, direction);
}

function isCodeTarget(path: string) {
  const normalized = path.replaceAll("\\", "/");
  const filename = normalized.split("/").at(-1) || "";
  const extensionIndex = filename.lastIndexOf(".");
  return extensionIndex >= 0 && CODE_FILE_EXTENSIONS.has(filename.slice(extensionIndex).toLowerCase());
}

function CommandOrb({ children, color = "#67d9f6" }: { children: string; color?: string }) {
  return <GlassIconOrb color={color} size={34} decorative><span>{children}</span></GlassIconOrb>;
}

function keepPressedChoiceVisible(container: HTMLDivElement | null) {
  const selected = container?.querySelector<HTMLElement>('[aria-pressed="true"]');
  if (!container || !selected) return;
  const containerRect = container.getBoundingClientRect();
  const selectedRect = selected.getBoundingClientRect();
  if (selectedRect.top < containerRect.top) container.scrollTop -= containerRect.top - selectedRect.top;
  if (selectedRect.bottom > containerRect.bottom) container.scrollTop += selectedRect.bottom - containerRect.bottom;
}

export function BrainTelemetry3DPage({
  execute,
  telemetryBundle,
  brains,
  selectedBrain,
  activeBrain,
  selectionAuthorityMode,
  brainColors,
  onSelectBrain,
  onJumpSQLite,
  onEscapeAdmin,
  railCollapsed,
  onRailCollapsedChange,
  onRailAction,
  profileName,
  profileAvatar,
  onOpenProfile,
  reducedMotion,
  fullViewPromptBar,
}: BrainTelemetry3DPageProps) {
  const executeRef = useRef(execute);
  const requestGenerationRef = useRef(0);
  const telemetryCacheRef = useRef<Record<string, CachedTelemetryState>>({});
  const orbFocusSequenceRef = useRef(0);
  const wheelFocusLockRef = useRef<number | null>(null);
  const wheelFocusGestureArmedRef = useRef(true);
  const mouseHistoryLockRef = useRef(false);
  const hoveredOrbIdRef = useRef<string | null>(null);
  const targetedOrbIdRef = useRef<string | null>(null);
  const focusedOrbIdRef = useRef<string | null>(null);
  const brainRailListRef = useRef<HTMLDivElement>(null);
  const deltaRailListRef = useRef<HTMLDivElement>(null);
  const fullViewHostRef = useRef<HTMLDivElement>(null);
  const sceneFrameRef = useRef<HTMLDivElement>(null);
  const fullViewRef = useRef(false);
  const fullViewModeRef = useRef<"BROWSER_FULLSCREEN" | "FOCUS_MODE" | "NONE">("NONE");
  const fullscreenToggleLockRef = useRef(false);
  const rendererResizeFrameRef = useRef<number | null>(null);
  const pendingStateProofRef = useRef("");
  const preservedStateSignatureRef = useRef("");
  const rendererResizeCountRef = useRef(0);
  const [contract, setContract] = useState<TelemetryContractResult | null>(null);
  const [snapshot, setSnapshot] = useState<TelemetrySnapshotResult | null>(null);
  const [deltas, setDeltas] = useState<TelemetryDelta[]>([]);
  const [deltaLoading, setDeltaLoading] = useState(true);
  const [deltaVersionCount, setDeltaVersionCount] = useState(0);
  const [graph, setGraph] = useState<TelemetryGraphResult | null>(null);
  const [selectedDeltaId, setSelectedDeltaId] = useState("");
  const [overlayEnabled, setOverlayEnabled] = useState(false);
  const [scopeHistory, setScopeHistory] = useState<Array<string | null>>([null]);
  const [historyIndex, setHistoryIndex] = useState(0);
  const [telemetrySelection, setTelemetrySelection] = useState<TelemetrySelection>("none");
  const [selectedNode, setSelectedNode] = useState<TelemetryNode | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<TelemetryRelationEdge | null>(null);
  const [cameraDistance, setCameraDistance] = useState(CAMERA_DEFAULT_DISTANCE);
  const [cameraResetNonce, setCameraResetNonce] = useState(0);
  const [hoveredOrbId, setHoveredOrbId] = useState<string | null>(null);
  const [targetedOrbId, setTargetedOrbId] = useState<string | null>(null);
  const [focusedOrbId, setFocusedOrbId] = useState<string | null>(null);
  const [orbFocusRequest, setOrbFocusRequest] = useState<TelemetryOrbFocusRequest | null>(null);
  const [orbFocusAudit, setOrbFocusAudit] = useState<TelemetryOrbFocusAudit | null>(null);
  const [orbRotationAudit, setOrbRotationAudit] = useState<TelemetryOrbRotationAudit | null>(null);
  const [orbLayoutAudit, setOrbLayoutAudit] = useState<TelemetryOrbLayoutAudit | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [snapshotLookupMs, setSnapshotLookupMs] = useState<number | null>(null);
  const [navigationLookupMs, setNavigationLookupMs] = useState<number | null>(null);
  const [snapshotLookupTier, setSnapshotLookupTier] = useState<"MEMORY_SNAPSHOT" | "DURABLE_SNAPSHOT" | "">("");
  const [isFullView, setIsFullView] = useState(false);
  const [fullViewMode, setFullViewMode] = useState<"BROWSER_FULLSCREEN" | "FOCUS_MODE" | "NONE">("NONE");
  const [fullViewDeltaCollapsed, setFullViewDeltaCollapsed] = useState(false);
  const [rendererResizeAudit, setRendererResizeAudit] = useState({
    clientWidth: 0,
    clientHeight: 0,
    backingWidth: 0,
    backingHeight: 0,
    aspect: 0,
    resizeCount: 0,
  });
  const [fullViewStateProof, setFullViewStateProof] = useState("PENDING");

  useEffect(() => {
    executeRef.current = execute;
  }, [execute]);

  useEffect(() => () => {
    if (wheelFocusLockRef.current !== null) window.clearTimeout(wheelFocusLockRef.current);
  }, []);

  useEffect(() => {
    focusedOrbIdRef.current = focusedOrbId;
  }, [focusedOrbId]);

  useEffect(() => {
    const frame = requestAnimationFrame(() => keepPressedChoiceVisible(brainRailListRef.current));
    return () => cancelAnimationFrame(frame);
  }, [selectedBrain, railCollapsed]);

  useEffect(() => {
    const frame = requestAnimationFrame(() => keepPressedChoiceVisible(deltaRailListRef.current));
    return () => cancelAnimationFrame(frame);
  }, [selectedDeltaId, deltaLoading, railCollapsed]);

  const selectedDelta = useMemo(
    () => deltas.find((delta) => delta.delta_id === selectedDeltaId) || null,
    [deltas, selectedDeltaId],
  );
  const currentScopeId = scopeHistory[historyIndex] ?? null;
  const visibleGraph = useMemo(() => {
    if (!graph) return null;
    const scopedGraph = currentScopeId ? {
      ...graph,
      scope: { scope_id: currentScopeId, mode: "REPLACED_WITH_INDEXED_SUBGRAPH" as const },
    } : {
      ...graph,
      scope: { scope_id: null, mode: "FULL_INDEXED_GRAPH" as const },
    };
    return buildTelemetryNeighborhood(scopedGraph);
  }, [currentScopeId, graph]);
  const zoomUnlocked = Boolean(visibleGraph);
  const selectedBrainColor = brainColors[selectedBrain] || "#67d9f6";
  const preservedStateSignature = useMemo(() => JSON.stringify({
    selectedBrain,
    activeBrain,
    selectedDeltaId,
    overlayEnabled,
    scopeHistory,
    historyIndex,
    telemetrySelection,
    selectedNodeId: selectedNode?.node_id || "",
    selectedEdgeId: selectedEdge?.edge_id || "",
    cameraDistance,
    focusedOrbId,
    targetedOrbId,
    cameraResetNonce,
    rotation: orbRotationAudit?.rotationAfter || [],
  }), [
    activeBrain,
    cameraDistance,
    cameraResetNonce,
    focusedOrbId,
    historyIndex,
    orbRotationAudit,
    overlayEnabled,
    scopeHistory,
    selectedBrain,
    selectedDeltaId,
    selectedEdge,
    selectedNode,
    targetedOrbId,
    telemetrySelection,
  ]);
  preservedStateSignatureRef.current = preservedStateSignature;

  function scheduleRendererResizeAudit() {
    if (rendererResizeFrameRef.current !== null) cancelAnimationFrame(rendererResizeFrameRef.current);
    window.dispatchEvent(new Event("resize"));
    rendererResizeFrameRef.current = requestAnimationFrame(() => {
      rendererResizeFrameRef.current = requestAnimationFrame(() => {
        rendererResizeFrameRef.current = null;
        const frame = sceneFrameRef.current;
        if (!frame) return;
        const canvas = frame.querySelector<HTMLCanvasElement>("canvas");
        const clientWidth = Math.max(0, Math.round(frame.clientWidth));
        const clientHeight = Math.max(0, Math.round(frame.clientHeight));
        rendererResizeCountRef.current += 1;
        setRendererResizeAudit({
          clientWidth,
          clientHeight,
          backingWidth: canvas?.width || 0,
          backingHeight: canvas?.height || 0,
          aspect: clientHeight > 0 ? clientWidth / clientHeight : 0,
          resizeCount: rendererResizeCountRef.current,
        });
      });
    });
  }

  function setFocusMode(active: boolean) {
    fullViewRef.current = active;
    fullViewModeRef.current = active ? "FOCUS_MODE" : "NONE";
    setIsFullView(active);
    setFullViewMode(active ? "FOCUS_MODE" : "NONE");
    scheduleRendererResizeAudit();
  }

  async function enterFullView() {
    if (fullViewRef.current || fullscreenToggleLockRef.current) return;
    fullscreenToggleLockRef.current = true;
    window.setTimeout(() => { fullscreenToggleLockRef.current = false; }, 320);
    pendingStateProofRef.current = preservedStateSignatureRef.current;
    const host = fullViewHostRef.current;
    if (!host) return;
    try {
      if (typeof host.requestFullscreen !== "function") throw new Error("FULLSCREEN_API_UNAVAILABLE");
      await host.requestFullscreen({ navigationUI: "hide" });
      if (document.fullscreenElement !== host) throw new Error("FULLSCREEN_PERMISSION_NOT_GRANTED");
      fullViewRef.current = true;
      fullViewModeRef.current = "BROWSER_FULLSCREEN";
      setIsFullView(true);
      setFullViewMode("BROWSER_FULLSCREEN");
      scheduleRendererResizeAudit();
    } catch {
      setFocusMode(true);
    }
  }

  async function exitFullView() {
    if (!fullViewRef.current || fullscreenToggleLockRef.current) return;
    fullscreenToggleLockRef.current = true;
    window.setTimeout(() => { fullscreenToggleLockRef.current = false; }, 320);
    pendingStateProofRef.current = preservedStateSignatureRef.current;
    const host = fullViewHostRef.current;
    if (document.fullscreenElement === host && typeof document.exitFullscreen === "function") {
      try {
        await document.exitFullscreen();
        return;
      } catch {
        // A browser may reject programmatic exit while already cancelling
        // fullscreen. The focus-mode reset below keeps the scene available.
      }
    }
    setFocusMode(false);
  }

  function toggleFullView() {
    if (fullViewRef.current) void exitFullView();
    else void enterFullView();
  }

  useEffect(() => {
    fullViewRef.current = isFullView;
    fullViewModeRef.current = fullViewMode;
    scheduleRendererResizeAudit();
    if (!pendingStateProofRef.current) return;
    const frame = requestAnimationFrame(() => {
      const preserved = pendingStateProofRef.current === preservedStateSignatureRef.current;
      setFullViewStateProof(preserved ? "PRESERVED" : "STATE_CHANGED_DURING_TOGGLE");
      pendingStateProofRef.current = "";
    });
    return () => cancelAnimationFrame(frame);
  }, [fullViewMode, isFullView]);

  useEffect(() => {
    const frame = sceneFrameRef.current;
    if (!frame || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => scheduleRendererResizeAudit());
    observer.observe(frame);
    scheduleRendererResizeAudit();
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    function onFullscreenChange() {
      const host = fullViewHostRef.current;
      if (host && document.fullscreenElement === host) {
        fullViewRef.current = true;
        fullViewModeRef.current = "BROWSER_FULLSCREEN";
        setIsFullView(true);
        setFullViewMode("BROWSER_FULLSCREEN");
        scheduleRendererResizeAudit();
        return;
      }
      if (fullViewModeRef.current === "BROWSER_FULLSCREEN") {
        fullViewRef.current = false;
        fullViewModeRef.current = "NONE";
        setIsFullView(false);
        setFullViewMode("NONE");
        scheduleRendererResizeAudit();
      }
    }

    function onFullViewKey(event: KeyboardEvent) {
      if (event.key === "F11") {
        if (event.repeat) return;
        event.preventDefault();
        event.stopPropagation();
        toggleFullView();
        return;
      }
      if (event.key === "Escape" && fullViewRef.current) {
        if (fullViewModeRef.current === "FOCUS_MODE") {
          event.preventDefault();
          event.stopPropagation();
        }
        void exitFullView();
      }
    }

    document.addEventListener("fullscreenchange", onFullscreenChange);
    window.addEventListener("keydown", onFullViewKey, true);
    return () => {
      document.removeEventListener("fullscreenchange", onFullscreenChange);
      window.removeEventListener("keydown", onFullViewKey, true);
    };
  }, []);

  useEffect(() => () => {
    if (rendererResizeFrameRef.current !== null) cancelAnimationFrame(rendererResizeFrameRef.current);
  }, []);

  async function fetchPersistedGraph(deltaId: string, generation = requestGenerationRef.current) {
    const receipt = await executeRef.current(
      "brain.telemetry.graph",
      deltaId ? "Saved candidate topology snapshot opened" : "Saved accepted topology snapshot opened",
      {
        brain_name: selectedBrain,
        delta_id: deltaId || undefined,
        max_nodes: 0,
      },
    );
    const nextGraph = telemetryResult<TelemetryGraphResult>(receipt, "brain.telemetry.graph");
    if (generation === requestGenerationRef.current) {
      setGraph(nextGraph);
      const cached = telemetryCacheRef.current[selectedBrain];
      if (cached) telemetryCacheRef.current[selectedBrain] = {
        ...cached,
        graphs: { ...cached.graphs, [deltaId || BASE_GRAPH_CACHE_KEY]: nextGraph },
      };
    }
    return nextGraph;
  }

  useEffect(() => {
    const generation = ++requestGenerationRef.current;
    const startedAt = performance.now();
    setError("");
    setOverlayEnabled(false);
    setScopeHistory([null]);
    setHistoryIndex(0);
    setTelemetrySelection("none");
    setSelectedNode(null);
    setSelectedEdge(null);
    setCameraDistance(CAMERA_DEFAULT_DISTANCE);
    hoveredOrbIdRef.current = null;
    targetedOrbIdRef.current = null;
    focusedOrbIdRef.current = null;
    setHoveredOrbId(null);
    setTargetedOrbId(null);
    setFocusedOrbId(null);
    setOrbFocusRequest(null);
    setOrbFocusAudit(null);
    setOrbRotationAudit(null);
    setOrbLayoutAudit(null);
    setCameraResetNonce((value) => value + 1);
    setNavigationLookupMs(null);
    if (!telemetryBundle
      || telemetryBundle.status !== "READY"
      || telemetryBundle.brain_name !== selectedBrain
      || !telemetryBundle.contract
      || !telemetryBundle.snapshot
      || !telemetryBundle.deltas
      || !telemetryBundle.graph) {
      setContract(null);
      setSnapshot(null);
      setDeltas([]);
      setDeltaVersionCount(0);
      setDeltaLoading(false);
      setGraph(null);
      setSelectedDeltaId("");
      setSnapshotLookupMs(0);
      setSnapshotLookupTier("");
      setLoading(false);
      setError(telemetryBundle?.error_code || "TELEMETRY_STORED_ACCEPTED_BUNDLE_REQUIRED");
      return;
    }

    // brain.select returned one already-materialized accepted topology bundle.
    // This is an ordinary state-travel read: zero scan, build, Refresh, Fuse,
    // delta generation, or topology mutation occurs here.
    const nextDeltas = telemetryBundle.deltas.deltas;
    const latestDelta = nextDeltas[nextDeltas.length - 1];
    const durableLookupMs = Math.max(0, performance.now() - startedAt);
    const cached: CachedTelemetryState = {
      contract: telemetryBundle.contract,
      snapshot: telemetryBundle.snapshot,
      deltas: nextDeltas,
      deltaVersionCount: telemetryBundle.deltas.version_count,
      graphs: { [BASE_GRAPH_CACHE_KEY]: telemetryBundle.graph },
      durableLookupMs,
    };
    telemetryCacheRef.current[selectedBrain] = cached;
    setContract(cached.contract);
    setSnapshot(cached.snapshot);
    setDeltas(cached.deltas);
    setDeltaVersionCount(cached.deltaVersionCount);
    setSelectedDeltaId(latestDelta?.delta_id || "");
    setDeltaLoading(false);
    setGraph(cached.graphs[BASE_GRAPH_CACHE_KEY]);
    setSnapshotLookupMs(durableLookupMs);
    setSnapshotLookupTier("DURABLE_SNAPSHOT");
    setLoading(false);
    if (generation !== requestGenerationRef.current) return;
  }, [selectedBrain, telemetryBundle]);

  function clearObjectSelection(nextSelection: TelemetrySelection = "none") {
    setTelemetrySelection(nextSelection);
    setSelectedNode(null);
    setSelectedEdge(null);
  }

  async function loadGraphVariant(deltaId: string) {
    const generation = ++requestGenerationRef.current;
    const cacheKey = deltaId || BASE_GRAPH_CACHE_KEY;
    const cachedGraph = telemetryCacheRef.current[selectedBrain]?.graphs[cacheKey];
    const startedAt = performance.now();
    setError("");
    if (cachedGraph) {
      setGraph(cachedGraph);
      setSnapshotLookupMs(Math.max(0, performance.now() - startedAt));
      setSnapshotLookupTier("MEMORY_SNAPSHOT");
      return;
    }
    setLoading(true);
    try {
      await fetchPersistedGraph(deltaId, generation);
      if (generation === requestGenerationRef.current) {
        setSnapshotLookupMs(Math.max(0, performance.now() - startedAt));
        setSnapshotLookupTier("DURABLE_SNAPSHOT");
      }
    } catch (reason) {
      if (generation === requestGenerationRef.current) setError(telemetryErrorCode(reason));
    } finally {
      if (generation === requestGenerationRef.current) setLoading(false);
    }
  }

  function measureSavedScopeLookup(scopeId: string | null) {
    const startedAt = performance.now();
    if (!graph) {
      setError("SAVED_TOPOLOGY_SNAPSHOT_UNAVAILABLE");
      return false;
    }
    if (scopeId && !graph.nodes.some((node) => node.node_id === scopeId)) {
      setError("SAVED_TOPOLOGY_SCOPE_NOT_FOUND");
      return false;
    }
    buildTelemetryNeighborhood(scopeId ? {
      ...graph,
      scope: { scope_id: scopeId, mode: "REPLACED_WITH_INDEXED_SUBGRAPH" },
    } : {
      ...graph,
      scope: { scope_id: null, mode: "FULL_INDEXED_GRAPH" },
    });
    setNavigationLookupMs(Math.max(0, performance.now() - startedAt));
    setError("");
    return true;
  }

  function truncateForwardHistory(nextScopeId: string) {
    const truncated = scopeHistory.slice(0, historyIndex + 1);
    const nextHistory = [...truncated, nextScopeId];
    setScopeHistory(nextHistory);
    setHistoryIndex(nextHistory.length - 1);
    return nextHistory;
  }

  function openFolder(node: TelemetryNode) {
    if (node.kind !== "folder" && node.kind !== "source") return;
    if (node.node_id === currentScopeId) return;
    if (!measureSavedScopeLookup(node.node_id)) return;
    truncateForwardHistory(node.node_id);
    clearObjectSelection();
    hoveredOrbIdRef.current = null;
    targetedOrbIdRef.current = null;
    focusedOrbIdRef.current = null;
    setHoveredOrbId(null);
    setTargetedOrbId(null);
    setFocusedOrbId(null);
    setOrbFocusRequest(null);
    setOrbFocusAudit(null);
    setOrbRotationAudit(null);
    setOrbLayoutAudit(null);
    setCameraResetNonce((value) => value + 1);
  }

  function navigateHistory(nextIndex: number) {
    if (nextIndex < 0 || nextIndex >= scopeHistory.length || nextIndex === historyIndex) return;
    const scopeId = scopeHistory[nextIndex] ?? null;
    if (!measureSavedScopeLookup(scopeId)) return;
    setHistoryIndex(nextIndex);
    clearObjectSelection();
    hoveredOrbIdRef.current = null;
    targetedOrbIdRef.current = null;
    focusedOrbIdRef.current = null;
    setHoveredOrbId(null);
    setTargetedOrbId(null);
    setFocusedOrbId(null);
    setOrbFocusRequest(null);
    setOrbFocusAudit(null);
    setOrbRotationAudit(null);
    setOrbLayoutAudit(null);
    setCameraResetNonce((value) => value + 1);
  }

  function goBack() {
    navigateHistory(historyIndex - 1);
  }

  function goForward() {
    navigateHistory(historyIndex + 1);
  }

  function onSceneMouseHistory(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.button !== 3 && event.button !== 4) return;
    event.preventDefault();
    event.stopPropagation();
    if (mouseHistoryLockRef.current) return;
    mouseHistoryLockRef.current = true;
    window.setTimeout(() => { mouseHistoryLockRef.current = false; }, 320);
    if (event.button === 3) goBack();
    else goForward();
  }

  function goRoot() {
    if (historyIndex === 0 && currentScopeId === null) return;
    if (!measureSavedScopeLookup(null)) return;
    setScopeHistory([null]);
    setHistoryIndex(0);
    clearObjectSelection();
    hoveredOrbIdRef.current = null;
    targetedOrbIdRef.current = null;
    focusedOrbIdRef.current = null;
    setHoveredOrbId(null);
    setTargetedOrbId(null);
    setFocusedOrbId(null);
    setOrbFocusRequest(null);
    setOrbFocusAudit(null);
    setOrbRotationAudit(null);
    setOrbLayoutAudit(null);
    setCameraResetNonce((value) => value + 1);
  }

  function onCanvasWheel(event: WheelEvent<HTMLDivElement>) {
    if (!visibleGraph) return;
    const wheelDelta = Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
    if (Math.abs(wheelDelta) < 1) return;
    event.preventDefault();
    event.stopPropagation();
    if (wheelFocusLockRef.current !== null) window.clearTimeout(wheelFocusLockRef.current);
    wheelFocusLockRef.current = window.setTimeout(() => {
      wheelFocusLockRef.current = null;
      wheelFocusGestureArmedRef.current = true;
    }, 760);
    if (!wheelFocusGestureArmedRef.current) return;
    wheelFocusGestureArmedRef.current = false;
    const hoverAnchor = focusedOrbIdRef.current || targetedOrbIdRef.current || hoveredOrbIdRef.current;
    requestNestedOrbFocus(wheelDelta < 0 ? "INWARD" : "OUTWARD", hoverAnchor);
  }

  function handleHoveredOrbChange(orbId: string | null) {
    hoveredOrbIdRef.current = orbId;
    setHoveredOrbId(orbId);
    if (orbId) {
      targetedOrbIdRef.current = orbId;
      setTargetedOrbId(orbId);
    }
  }

  function nestedOrbIds() {
    return orbLayoutAudit?.orderedShellIds || [];
  }

  function requestNestedOrbFocus(
    direction: "INWARD" | "OUTWARD",
    hoverAnchor = targetedOrbIdRef.current || hoveredOrbIdRef.current || focusedOrbIdRef.current,
  ) {
    const orderedOrbIds = nestedOrbIds();
    const currentFocusedOrbId = focusedOrbIdRef.current || focusedOrbId;
    const nextFocusedOrbId = resolveNestedOrbFocus(orderedOrbIds, currentFocusedOrbId, hoverAnchor, direction);
    if (!nextFocusedOrbId) {
      setError("TELEMETRY_ORB_TARGET_REQUIRED");
      return;
    }
    hoveredOrbIdRef.current = null;
    setHoveredOrbId(null);
    targetedOrbIdRef.current = nextFocusedOrbId;
    setTargetedOrbId(nextFocusedOrbId);
    focusedOrbIdRef.current = nextFocusedOrbId;
    setFocusedOrbId(nextFocusedOrbId);
    setOrbFocusRequest({
      orbId: currentFocusedOrbId || hoverAnchor || nextFocusedOrbId,
      nextFocusedOrbId,
      direction,
      nonce: ++orbFocusSequenceRef.current,
    });
  }

  function chooseDelta(delta: TelemetryDelta) {
    setSelectedDeltaId(delta.delta_id);
    clearObjectSelection();
    if (overlayEnabled) void loadGraphVariant(delta.delta_id);
  }

  function chooseVisualizedDelta(delta: TelemetryDelta) {
    setSelectedDeltaId(delta.delta_id);
    setOverlayEnabled(true);
    clearObjectSelection();
    void loadGraphVariant(delta.delta_id);
  }

  function toggleOverlay() {
    if (overlayEnabled) {
      setOverlayEnabled(false);
      clearObjectSelection();
      void loadGraphVariant("");
      return;
    }
    if (!selectedDeltaId) {
      setError("TELEMETRY_DELTA_SELECTION_REQUIRED");
      return;
    }
    setOverlayEnabled(true);
    clearObjectSelection();
    void loadGraphVariant(selectedDeltaId);
  }

  async function openTarget(node: TelemetryNode) {
    const target = telemetryNodePath(node);
    if (!target) {
      setError("TELEMETRY_TARGET_NOT_INDEXED");
      return;
    }
    setError("");
    const codeTarget = node.kind === "file" && isCodeTarget(target);
    const application = codeTarget ? "vscode" : "default";
    const destination = node.kind !== "file"
      ? "File Explorer"
      : codeTarget ? "Visual Studio Code" : "the Windows owning app";
    const receipt = await executeRef.current(
      "brain.telemetry.openTarget",
      `${node.label} opened in ${destination}`,
      { brain_name: selectedBrain, target, application },
    );
    if (!receipt.ok) setError(telemetryErrorCode(receipt.error));
  }

  async function chooseBrain(name: string) {
    if (name === selectedBrain) {
      const startedAt = performance.now();
      const cached = telemetryCacheRef.current[name];
      if (cached) {
        setSnapshotLookupMs(Math.max(0, performance.now() - startedAt));
        setSnapshotLookupTier("MEMORY_SNAPSHOT");
      }
      return;
    }
    setLoading(true);
    setError("");
    try {
      await onSelectBrain(name);
    } catch (reason) {
      setError(telemetryErrorCode(reason));
      setLoading(false);
    }
  }

  const railProfileAvatar = profileAvatar || (
    <GlassIconOrb className="cockpit-profile-avatar" color="#82d9ec" size={30} decorative>
      <span>{profileName.slice(0, 2).toUpperCase()}</span>
    </GlassIconOrb>
  );
  const expandedRail = (
    <ExpandedSidePanelShell className="cockpit-side telemetry3d-side" onToggle={() => onRailCollapsedChange(true)}>
      <section className="telemetry3d-rail-section telemetry3d-rail-section--brains" aria-label="Available brains">
        <strong>Available Brains</strong>
        <div ref={brainRailListRef} className="telemetry3d-rail-list telemetry3d-selector-strip brain-strip" data-visible-rows="3" data-scroll-owner="brains">
          {brains.map((brain) => <GlassPill
            key={brain}
            className={`telemetry3d-rail-choice telemetry3d-brain-choice ${brain === activeBrain ? "is-active" : ""} ${brain === selectedBrain ? "is-selected" : ""}`}
            geometrySchema="side-rail"
            leading={<BrainGlassSphere size={38} color={brainColors[brain] || "#67d9f6"} />}
            onClick={() => void chooseBrain(brain)}
            aria-pressed={brain === selectedBrain}
          >
            <span title={brain}>{brain}<small>{brain === activeBrain ? "connected brain" : brain === selectedBrain ? "viewing · backend unbound" : "registered brain"}</small></span>
          </GlassPill>)}
        </div>
      </section>

      <section
        className="telemetry3d-rail-section telemetry3d-rail-section--deltas"
        aria-label="Immutable version deltas"
        data-delta-owner={selectedBrain}
        data-delta-load-state={deltaLoading ? "loading" : deltas.length > 0 ? "ready" : "baseline-only"}
      >
        <strong>Brain Deltas</strong>
        <div ref={deltaRailListRef} className="telemetry3d-rail-list telemetry3d-selector-strip delta-strip" data-visible-rows="3" data-scroll-owner="deltas">
          {deltaLoading && <span className="telemetry3d-empty-strip telemetry3d-empty-strip--loading">
            Opening saved <strong>{selectedBrain}</strong> Delta lineage…
          </span>}
          {!deltaLoading && deltas.length === 0 && <span className="telemetry3d-empty-strip">
            <strong>{selectedBrain} · Δ0</strong>
            <small>{deltaVersionCount > 0
              ? `${selectedBrain} immutable baseline; no prior version exists to form Δ1.`
              : `${selectedBrain} has no immutable version indexed yet.`}</small>
          </span>}
          {deltas.map((delta) => <GlassPill
            key={delta.delta_id}
            className={`telemetry3d-rail-choice telemetry3d-delta-choice ${delta.delta_id === selectedDeltaId ? "is-selected is-active" : ""}`}
            geometrySchema="side-rail"
            leading={<GlassIconOrb color={delta.tone === "RED" ? "#ff6076" : delta.tone === "GREEN" ? "#43d99a" : "#73d9f6"} size={34} decorative><span>Δ</span></GlassIconOrb>}
            onClick={() => chooseDelta(delta)}
            aria-pressed={delta.delta_id === selectedDeltaId}
            title={`${delta.before_version_id} → ${delta.after_version_id}`}
          >
            <span>{delta.display_name}<small>{delta.changed_object_count} indexed changes</small></span>
          </GlassPill>)}
        </div>
      </section>

      <GlassPill className="cockpit-active-session telemetry3d-connected-pill" geometrySchema="side-rail" data-brain-selection-authority={selectionAuthorityMode} leading={<BrainGlassSphere size={30} color={brainColors[activeBrain || selectedBrain] || selectedBrainColor} />}>
        <span>{activeBrain ? "Connected to" : "Viewing only"}<small>{activeBrain || `${selectedBrain} · backend unavailable`}</small></span>
      </GlassPill>
      <GlassPill
        className="cockpit-profile-pill telemetry3d-profile-pill"
        geometrySchema="side-rail"
        leading={railProfileAvatar}
        trailing={<GlassIconOrb className="cockpit-settings-orb" color="#69d9f5" size={40} decorative><span>⚙</span></GlassIconOrb>}
        onClick={onOpenProfile}
      >
        <span>{profileName}<small>Profile &amp; Settings</small></span>
      </GlassPill>
    </ExpandedSidePanelShell>
  );
  const collapsedRail = (
    <CollapsedSidePanelShell
      className="cockpit-side telemetry3d-side"
      onToggle={() => onRailCollapsedChange(false)}
      onAction={onRailAction}
    >
      <div className="cockpit-side__collapsed-bottom">
        <GlassPill iconOnly leading={<BrainGlassSphere size={30} color={brainColors[activeBrain || selectedBrain] || selectedBrainColor} />} width={44} minHeight={44} aria-label={activeBrain ? `${activeBrain} connected brain` : `${selectedBrain} viewing only; backend unavailable`} />
        <GlassPill iconOnly leading={railProfileAvatar} width={44} minHeight={44} aria-label={`${profileName} profile and settings`} onClick={onOpenProfile} />
      </div>
    </CollapsedSidePanelShell>
  );

  return (
    <div
      ref={fullViewHostRef}
      className={`telemetry3d-fullview-host ${isFullView ? "is-full-view" : ""} ${fullViewMode === "FOCUS_MODE" ? "is-focus-mode" : ""}`}
      data-full-view-active={isFullView ? "true" : "false"}
      data-full-view-mode={fullViewMode}
      data-full-view-state-proof={fullViewStateProof}
      data-full-view-listener-count="1"
      data-full-view-brain-switching="UNAVAILABLE"
      data-full-view-source-reads="0"
      data-full-view-topology-rebuilds="0"
      data-full-view-selected-brain={selectedBrain}
      data-full-view-selected-delta={selectedDeltaId || "NONE"}
      data-full-view-focused-orb={focusedOrbId || "NONE"}
      data-full-view-history-index={historyIndex}
      data-renderer-client-size={`${rendererResizeAudit.clientWidth}x${rendererResizeAudit.clientHeight}`}
      data-renderer-backing-size={`${rendererResizeAudit.backingWidth}x${rendererResizeAudit.backingHeight}`}
      data-renderer-camera-aspect={rendererResizeAudit.aspect.toFixed(6)}
      data-renderer-resize-count={rendererResizeAudit.resizeCount}
      data-renderer-resize-law="RESIZE_OBSERVER_WINDOW_RESIZE_CAMERA_PROJECTION"
    >
    <WorkspaceShell
      className={`telemetry3d-workspace cockpit-frame ${railCollapsed ? "is-collapsed" : "is-expanded"}`}
      sideMode={railCollapsed ? "collapsed" : "expanded"}
      label="Evidence Lane 3D telemetry workspace"
    >
      {railCollapsed ? collapsedRail : expandedRail}
      <section className="telemetry3d-main">
        <EvidenceHeaderShell
          activeModule="telemetry3d"
          onModuleSwitch={(module) => { if (module === "sqlite") onJumpSQLite(); }}
          onEscapeAdmin={onEscapeAdmin}
        />
        <section
      className="telemetry3d-page"
      data-telemetry-side-rail={railCollapsed ? "collapsed" : "expanded"}
      data-telemetry-main-container-count="1"
      data-command-zoom-gate="saved-topology-required"
      data-command-zoom-law="ONE_NESTED_GLASS_ORB_FOCUS_STEP"
      data-canvas-wheel-law="CAMERA_SCENE_POINT_A_TO_POINT_B_UNDER_CURSOR_NO_CLICK"
      data-wheel-action-law="ONE_GOVERNED_ACTION_ONE_NESTED_ORB_STEP"
      data-wheel-focus-anchor="FOCUSED_INDEXED_SCENE_SHELL_HOVER_ONLY_INITIAL"
      data-wheel-topology-requests="ZERO_BY_DESIGN"
      data-hovered-orb-id={hoveredOrbId || "NONE"}
      data-focused-orb-id={focusedOrbId || "NONE"}
      data-targeted-orb-id={targetedOrbId || "NONE"}
      data-last-focus-orb-id={orbFocusAudit?.focusedOrbId || "NONE"}
      data-last-focus-direction={orbFocusAudit?.direction || "NONE"}
      data-last-focus-layers={orbFocusAudit ? `${orbFocusAudit.focusedLayerIndexBefore}->${orbFocusAudit.focusedLayerIndexAfter}` : ""}
      data-last-focus-scale={orbFocusAudit?.nestedFocusScale ?? ""}
      data-last-outer-wrap-scale={orbFocusAudit?.outerWrapScale ?? ""}
      data-last-focus-moved={orbFocusAudit ? String(orbFocusAudit.moved) : ""}
      data-last-focus-orientation-preserved={orbFocusAudit ? String(orbFocusAudit.orientationPreserved) : ""}
      data-last-focus-source-reads={orbFocusAudit?.sourceReadsDuringFocus ?? ""}
      data-data-derived-orb-count={orbLayoutAudit?.orbCount ?? ""}
      data-data-derived-orb-distribution={orbLayoutAudit?.distribution.join(",") || ""}
      data-scene-focus-stop-count={orbLayoutAudit?.focusStopCount ?? ""}
      data-folder-orb-count={orbLayoutAudit?.folderOrbCount ?? ""}
      data-edge-orb-count={orbLayoutAudit?.edgeOrbCount ?? ""}
      data-edge-members-per-orb={orbLayoutAudit?.edgeMembersPerOrb ?? ""}
      data-file-edge-node-count={orbLayoutAudit?.fileEdgeNodeCount ?? ""}
      data-child-folder-node-count={orbLayoutAudit?.childFolderNodeCount ?? ""}
      data-file-edge-orb-distribution={orbLayoutAudit?.fileEdgeDistribution.join(",") || ""}
      data-scene-orb-distribution={orbLayoutAudit?.sceneDistribution.join(",") || ""}
      data-scene-transition-count={orbLayoutAudit?.sceneTransitionCount ?? ""}
      data-scene-ordered-shell-ids={orbLayoutAudit?.orderedShellIds.join("|") || ""}
      data-scene-index-cache-key={orbLayoutAudit?.sceneIndexCacheKey || ""}
      data-scene-index-cache-status={orbLayoutAudit?.sceneIndexCacheStatus || ""}
      data-topology-snapshot-hash={orbLayoutAudit?.topologySnapshotHash || ""}
      data-layout-node-loss-count={orbLayoutAudit?.nodeLossCount ?? ""}
      data-layout-node-duplicate-count={orbLayoutAudit?.nodeDuplicateCount ?? ""}
      data-terminal-root-orb-id={orbLayoutAudit?.terminalRootOrbId || ""}
      data-terminal-root-layer-index={orbLayoutAudit?.terminalRootLayerIndex ?? ""}
      data-terminal-root-non-rotating={orbLayoutAudit ? String(orbLayoutAudit.terminalRootNonRotating) : ""}
      data-layout-saved-membership-count={orbLayoutAudit?.savedMembershipCount ?? ""}
      data-layout-source-reads={orbLayoutAudit?.sourceReadsDuringLayout ?? ""}
      data-last-rotation-orb-id={orbRotationAudit?.orbId || "NONE"}
      data-last-rotation-delta={orbRotationAudit ? `${orbRotationAudit.dragDeltaX},${orbRotationAudit.dragDeltaY},${orbRotationAudit.dragDeltaZ}` : ""}
      data-last-rotation-before={orbRotationAudit?.rotationBefore.join(",") || ""}
      data-last-rotation-after={orbRotationAudit?.rotationAfter.join(",") || ""}
      data-last-rotation-other-layers-changed={orbRotationAudit?.otherLayersChanged ?? ""}
      data-last-rotation-source-reads={orbRotationAudit?.sourceReadsDuringNavigation ?? ""}
      data-selector-layer-ratio="75-percent-of-prior"
      data-selected-brain={selectedBrain}
      data-active-brain={activeBrain || "UNBOUND"}
      data-selection-authority={selectionAuthorityMode}
      data-brain-switch-io-law="ATOMIC_STORED_BUNDLE_ZERO_SCAN_ZERO_BUILD_ZERO_DELTA"
      data-ordinary-view-mutation-count="0"
      data-snapshot-lookup-tier={snapshotLookupTier || "PENDING"}
      data-snapshot-lookup-ms={snapshotLookupMs === null ? "" : snapshotLookupMs.toFixed(3)}
      data-folder-lookup-ms={navigationLookupMs === null ? "" : navigationLookupMs.toFixed(3)}
      data-raw-project-files-reread={graph?.raw_project_files_reread ?? "UNAVAILABLE"}
      data-navigation-source="SAVED_BUILD_OR_FUSE_TOPOLOGY_SNAPSHOT"
      data-snapshot-integrity-status={snapshot?.status || "PENDING"}
      style={{ "--telemetry-brain-color": selectedBrainColor } as CSSProperties}
      aria-label="3D Brain Telemetry"
    >
      <GlassShell className="telemetry3d-main-container" label="Read-only saved-snapshot 3D brain telemetry">
        <header className="telemetry3d-title-row">
          <div>
            <span>3D BRAIN TELEMETRY</span>
            {selectedBrain && <em data-selected-brain-label="beside-3d">{selectedBrain}</em>}
            <strong>→ Full View</strong>
            <GlassPill
              className="telemetry3d-fullview-enter"
              leading={<CommandOrb>⛶</CommandOrb>}
              onClick={() => void enterFullView()}
              aria-label="Enter 3D Telemetry Full View"
              title="Enter Full View (F11)"
            >Full View</GlassPill>
          </div>
          <div className="telemetry3d-truth-status" data-status={error ? "error" : loading ? "loading" : "verified"}>
            <i aria-hidden="true" />
            <span>{error || (loading
              ? "STORED SNAPSHOT PENDING"
              : `SAVED SNAPSHOT READY · ${snapshotLookupMs === null ? "--" : snapshotLookupMs.toFixed(2)} ms`)}</span>
          </div>
        </header>

        <div
          ref={sceneFrameRef}
          className="telemetry3d-scene-frame"
          data-scene-selected={telemetrySelection === "scene" ? "true" : "false"}
          data-hovered-orb={hoveredOrbId ? "true" : "false"}
          data-active-orb-hovered={hoveredOrbId && hoveredOrbId === focusedOrbId ? "true" : "false"}
          data-topology-panel-ratio="at-least-80-percent"
          data-initial-projection="3d-independent-data-orbs"
          data-topology-layout="FIXED_OUTER_FOLDER_ORB_EDGE_ORBS_MAX_20_TERMINAL_ROOT_BUTTON"
          data-node-asset-law="LIGHTWEIGHT_TRANSPARENT_ROOT_FOLDER_EDGE_ASSETS_INSIDE_REAL_3D_GLASS"
          data-relation-geometry="NONE_USER_DEFERRED"
          data-connector-line-count="0"
          data-structural-shell-authority="HUMAN_EYE_LENS_REFRACTION_WITH_Z_DEPTH"
          data-reference-image-use="USER_SUPPLIED_TRANSPARENT_PNG_RUNTIME_GLBS_REFERENCE_ONLY"
          data-rotation-law="ACTIVE_TOPOLOGY_ORB_ONLY_FREE_XYZ_TERMINAL_ROOT_BUTTON_NON_ROTATING"
          data-depth-law="FIXED_CONCENTRIC_ORBS_CAMERA_MOVES_OUTER_TO_INNER"
          data-focus-travel-law="CAMERA_INTERPOLATION_POINT_A_TO_POINT_B_NO_ORB_POP"
          data-outer-neon-law="SELECTED_BRAIN_COLOR_FIXED_2D_OUTER_RIM_ONLY_GLASS_VOLUME_TRANSPARENT"
          data-wheel-focus-anchor="FOCUSED_INDEXED_SCENE_SHELL_HOVER_ONLY_INITIAL"
          data-mouse-history-law="BUTTON_3_BACK_BUTTON_4_FORWARD"
          onWheelCapture={onCanvasWheel}
          onMouseDownCapture={onSceneMouseHistory}
          onAuxClickCapture={onSceneMouseHistory}
        >
          <div className={`telemetry3d-ambient-brain ${reducedMotion ? "is-motion-reduced" : ""}`} aria-hidden="true">
            <BrainGlassSphere size="clamp(260px, 34vw, 520px)" color="#dff8fc" />
          </div>
          <aside
            className={`telemetry3d-fullview-delta ${fullViewDeltaCollapsed ? "is-collapsed" : ""}`}
            aria-label="Refresh Delta overlay selector"
            aria-hidden={!isFullView}
            data-full-view-control="refresh-delta-selector"
            data-layout-law="HORIZONTAL_LANDSCAPE_VERTICAL_PORTRAIT"
          >
            <header>
              <span>REFRESH DELTA</span>
              <button
                type="button"
                onClick={() => setFullViewDeltaCollapsed((current) => !current)}
                aria-expanded={!fullViewDeltaCollapsed}
                aria-label={fullViewDeltaCollapsed ? "Expand Refresh Delta selector" : "Collapse Refresh Delta selector"}
              >{fullViewDeltaCollapsed ? "＋" : "−"}</button>
            </header>
            <div className="telemetry3d-fullview-delta__choices">
              <GlassPill
                className="telemetry3d-fullview-delta__overlay"
                disabled={!overlayEnabled && !selectedDeltaId}
                state={overlayEnabled ? "active" : "idle"}
                tone={overlayEnabled ? "cyan" : "neutral"}
                leading={<CommandOrb color={overlayEnabled ? "#54d99f" : "#67d9f6"}>Δ</CommandOrb>}
                onClick={toggleOverlay}
              >{overlayEnabled ? "Overlay on" : "Overlay off"}</GlassPill>
              {deltaLoading && <span className="telemetry3d-fullview-delta__empty">Opening saved Refresh Delta lineage…</span>}
              {!deltaLoading && deltas.length === 0 && <span className="telemetry3d-fullview-delta__empty">No Refresh Delta available</span>}
              {deltas.map((delta) => <GlassPill
                key={`full-view-${delta.delta_id}`}
                className={`telemetry3d-fullview-delta__choice ${delta.delta_id === selectedDeltaId ? "is-selected is-active" : ""}`}
                leading={<GlassIconOrb color={delta.tone === "RED" ? "#ff6076" : delta.tone === "GREEN" ? "#43d99a" : "#73d9f6"} size={28} decorative><span>Δ</span></GlassIconOrb>}
                onClick={() => chooseVisualizedDelta(delta)}
                aria-pressed={delta.delta_id === selectedDeltaId}
                title={`${delta.display_name} · ${delta.before_version_id} → ${delta.after_version_id}`}
              ><span>{delta.display_name}<small>{delta.changed_object_count} changes</small></span></GlassPill>)}
            </div>
          </aside>

          <GlassPill
            className="telemetry3d-fullview-exit"
            leading={<CommandOrb>×</CommandOrb>}
            onClick={() => void exitFullView()}
            aria-label="Exit 3D Telemetry Full View"
            aria-hidden={!isFullView}
            data-full-view-control="exit"
            title="Exit Full View (Esc or F11)"
          >Exit Full View</GlassPill>

          <div
            className="telemetry3d-fullview-prompt"
            aria-hidden={!isFullView}
            data-full-view-control="prompt-bar"
            data-prompt-state-law="PERSISTENT_MOUNT_NO_ENTER_EXIT_RESET"
          >{fullViewPromptBar}</div>
          {visibleGraph ? <BrainTelemetryScene
            graph={visibleGraph}
            overlayEnabled={overlayEnabled}
            selectedNodeId={selectedNode?.node_id || ""}
            selectedEdgeId={selectedEdge?.edge_id || ""}
            cameraDistance={cameraDistance}
            cameraResetNonce={cameraResetNonce}
            focusRequest={orbFocusRequest}
            focusedOrbId={focusedOrbId}
            onFocusAudit={setOrbFocusAudit}
            onRotationAudit={setOrbRotationAudit}
            onLayoutAudit={(audit) => {
              setOrbLayoutAudit(audit);
              const firstOrbId = audit.orderedShellIds[0] || null;
              setFocusedOrbId((current) => {
                const next = current && audit.orderedShellIds.includes(current) ? current : firstOrbId;
                targetedOrbIdRef.current = targetedOrbIdRef.current && audit.orderedShellIds.includes(targetedOrbIdRef.current)
                  ? targetedOrbIdRef.current
                  : next;
                focusedOrbIdRef.current = next;
                return next;
              });
            }}
            onHoveredOrbChange={handleHoveredOrbChange}
            reducedMotion={reducedMotion}
            brainColor={selectedBrainColor}
            onSelectScene={() => clearObjectSelection("scene")}
            onSelectNode={(node) => {
              setTelemetrySelection("node");
              setSelectedNode(node);
              setSelectedEdge(null);
            }}
            onSelectEdge={(edge) => {
              setTelemetrySelection("edge");
              setSelectedEdge(edge);
              setSelectedNode(null);
            }}
            onOpenFolder={openFolder}
            onOpenTarget={(node) => void openTarget(node)}
          /> : <div className="telemetry3d-scene-empty" role="status">
            <strong>{loading ? "Stored topology pending" : "Telemetry unavailable"}</strong>
            <span>{error || "No demo topology is substituted."}</span>
          </div>}

          <aside className="telemetry3d-selection-readout" aria-live="polite">
            <span>{telemetrySelection === "scene" ? "WHOLE TELEMETRY SELECTED" : telemetrySelection.toUpperCase()}</span>
            <strong>{selectedNode?.label || selectedEdge?.relation_type || visibleGraph?.nodes[0]?.label || "Root indexed folder"}</strong>
            <small>{zoomUnlocked ? "Independent orb XYZ drag + nested orb focus traversal" : "Saved topology unavailable"}</small>
            <small className="telemetry3d-timing-proof">Snapshot {snapshotLookupMs === null ? "--" : `${snapshotLookupMs.toFixed(2)} ms`} · folder {navigationLookupMs === null ? "--" : `${navigationLookupMs.toFixed(3)} ms`} · source reads {graph?.raw_project_files_reread ?? "--"}</small>
          </aside>

          <aside className="telemetry3d-overlay-legend" data-overlay-enabled={overlayEnabled ? "true" : "false"}>
            <span><i className="is-green" /> changed / passed</span>
            <span><i className="is-red" /> failed / fallout</span>
            <small>{overlayEnabled && selectedDelta ? `${selectedDelta.display_name} direct indexed evidence` : "Overlay off — neutral topology"}</small>
          </aside>
        </div>

        <footer className="telemetry3d-proof-row">
          <span>{contract?.read_only === true ? "READ ONLY" : "CONTRACT PENDING"}</span>
          <span>{visibleGraph ? `${Math.max(0, visibleGraph.counts.nodes_returned - 1)} immediate children` : "0 children"}</span>
          <span>{visibleGraph ? `${visibleGraph.counts.edges_returned} indexed relations · connector geometry deferred` : "0 relations"}</span>
          <span>{visibleGraph ? "CURRENT ROOT + ALL IMMEDIATE FOLDERS / FILES" : "CURRENT ROOT PENDING"}</span>
        </footer>
      </GlassShell>

      <GlassShell className="telemetry3d-command-bar" as="nav" label="3D telemetry command bar" pillCluster="prompt-bar">
        <GlassPill disabled={!zoomUnlocked} leading={<CommandOrb>＋</CommandOrb>} onClick={() => requestNestedOrbFocus("INWARD")}>Zoom In</GlassPill>
        <GlassPill disabled={!zoomUnlocked} leading={<CommandOrb>−</CommandOrb>} onClick={() => requestNestedOrbFocus("OUTWARD")}>Zoom Out</GlassPill>
        <GlassPill disabled={historyIndex <= 0} leading={<CommandOrb>←</CommandOrb>} onClick={goBack}>Back</GlassPill>
        <GlassPill disabled={historyIndex >= scopeHistory.length - 1} leading={<CommandOrb>→</CommandOrb>} onClick={goForward}>Forward</GlassPill>
        <GlassPill disabled={historyIndex === 0} leading={<CommandOrb>⌂</CommandOrb>} onClick={goRoot}>Main root folder</GlassPill>
        <GlassPill
          disabled={!overlayEnabled && !selectedDeltaId}
          state={overlayEnabled ? "active" : "idle"}
          tone={overlayEnabled ? "cyan" : "neutral"}
          leading={<CommandOrb color={overlayEnabled ? "#54d99f" : "#67d9f6"}>Δ</CommandOrb>}
          onClick={toggleOverlay}
        >Delta overlay</GlassPill>
        <GlassPill leading={<CommandOrb color="#efd28a">DB</CommandOrb>} onClick={onJumpSQLite}>Jump to SQLite Builder</GlassPill>
      </GlassShell>
        </section>
      </section>
    </WorkspaceShell>
    </div>
  );
}
