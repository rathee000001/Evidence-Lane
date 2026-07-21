import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { t023CharacterProfileManifest, t023FutureRouteStates } from "../contracts/T023UiManifests";
import {
  t023UiSystemSchemas,
  universalTaskSurfaceIdentity,
  type SelectedBrainAuthorityMode,
  type UniversalProcessTaskEvent,
} from "../contracts/T023UniversalUiSystemSchemas";
import { toolLensPhaseDelayMs } from "../motion/ToolLensMotionTiming";
import { createNativeEvidenceAdapter, evidenceActionRegistry, evidenceLanePickerPolicy, evidenceSourceLanes, universalSourceSchema, type EvidenceLane, type EvidencePopup, type EvidenceUiCommand, type SimulationReceipt } from "../simulation/EvidenceOSSimulationContract";
import type { PersistedTelemetryBundle } from "../simulation/BrainTelemetryContract";
import { sqliteGlassTheme, sqliteGlassThemeStyle } from "../theme/EvidenceTheme";
import { universalPopupFadeMotion } from "../theme/UniversalPopupFade";
import { BrainGlassSphere } from "./BrainGlassSphere";
import { BrainTelemetry3DPage } from "./BrainTelemetry3DPage";
import { CollapsedSidePanelShell } from "./CollapsedSidePanelShell";
import { CommandIdentityOrb } from "./CommandIdentityOrb";
import { EvidenceHeaderShell } from "./EvidenceHeaderShell";
import { ExpandedSidePanelShell } from "./ExpandedSidePanelShell";
import { GlassIconOrb } from "./GlassIconOrb";
import { GlassPill } from "./GlassPill";
import { GlassShell } from "./GlassShell";
import { LiveMetricsClusterPanel } from "./LiveMetricsClusterPanel";
import { PulsatingBrain, type BrainMotionState } from "./PulsatingBrain";
import { SQLiteCommandBar, type CommandKey } from "./SQLiteCommandBar";
import { SourceLaneIcon } from "./SourceLaneIcon";
import { SystemTaskStatus, type TaskAction, type TaskQueueItem } from "./SystemTaskStatus";
import { evidenceToolFlow, ToolchainFlow, type ToolFlowPhase, type ToolFlowSubject } from "./ToolchainFlow";
import { WorkspaceShell } from "./WorkspaceShell";

const phaseNext: Partial<Record<ToolFlowPhase, ToolFlowPhase>> = {
  "flying-to-lens": "scanning",
  validated: "flying-to-brain",
  "flying-to-brain": "digesting",
};

function SettingsIcon() {
  return <svg
    className="cockpit-settings-icon"
    data-orb-icon="settings"
    viewBox="0 0 24 24"
    aria-hidden="true"
    focusable="false"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
  >
    <circle cx="12" cy="12" r="6.25" />
    <circle cx="12" cy="12" r="2.35" />
    <path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.28 5.28 7.4 7.4M16.6 16.6l2.12 2.12M18.72 5.28 16.6 7.4M7.4 16.6l-2.12 2.12" />
  </svg>;
}

const FIRST_M0_HIL_PASSED = false;

const initialBrains = ["New Brain 5", "New Brain 4", "New Brain 3", "New Brain 2", "Open UI", "RIL"];
const initialDefaultOutputRoot = "";
const T023_SIMULATION_PROFILE_STORAGE_V1 = "evidenceos.t023.simulation.profile.v1";

type StoredSimulationProfile = {
  profileName: string;
  profileRole: string;
  profileImageDataUrl: string;
  profileImageName: string;
  characterFaceGlbDataUrl: string;
  characterFaceGlbName: string;
  characterFaceGlbSha256: string;
  characterBodyGlbDataUrl: string;
  characterBodyGlbName: string;
  characterBodyGlbSha256: string;
};

const defaultSimulationProfile: StoredSimulationProfile = {
  profileName: "Evidence Lane Operator",
  profileRole: "Local Evidence Lane profile",
  profileImageDataUrl: "",
  profileImageName: "",
  characterFaceGlbDataUrl: "",
  characterFaceGlbName: "",
  characterFaceGlbSha256: "",
  characterBodyGlbDataUrl: "",
  characterBodyGlbName: "",
  characterBodyGlbSha256: "",
};

function loadSimulationProfile(): StoredSimulationProfile {
  try {
    const value = JSON.parse(localStorage.getItem(T023_SIMULATION_PROFILE_STORAGE_V1) || "{}") as Partial<StoredSimulationProfile>;
    return {
      profileName: typeof value.profileName === "string" && value.profileName.trim() ? value.profileName : defaultSimulationProfile.profileName,
      profileRole: typeof value.profileRole === "string" && value.profileRole.trim() ? value.profileRole : defaultSimulationProfile.profileRole,
      profileImageDataUrl: typeof value.profileImageDataUrl === "string" && value.profileImageDataUrl.startsWith("data:image/") ? value.profileImageDataUrl : "",
      profileImageName: typeof value.profileImageName === "string" ? value.profileImageName : "",
      characterFaceGlbDataUrl: typeof value.characterFaceGlbDataUrl === "string" && value.characterFaceGlbDataUrl.startsWith("data:") ? value.characterFaceGlbDataUrl : "",
      characterFaceGlbName: typeof value.characterFaceGlbName === "string" ? value.characterFaceGlbName : "",
      characterFaceGlbSha256: typeof value.characterFaceGlbSha256 === "string" && /^[a-f0-9]{64}$/i.test(value.characterFaceGlbSha256) ? value.characterFaceGlbSha256.toLowerCase() : "",
      characterBodyGlbDataUrl: typeof value.characterBodyGlbDataUrl === "string" && value.characterBodyGlbDataUrl.startsWith("data:") ? value.characterBodyGlbDataUrl : "",
      characterBodyGlbName: typeof value.characterBodyGlbName === "string" ? value.characterBodyGlbName : "",
      characterBodyGlbSha256: typeof value.characterBodyGlbSha256 === "string" && /^[a-f0-9]{64}$/i.test(value.characterBodyGlbSha256) ? value.characterBodyGlbSha256.toLowerCase() : "",
    };
  } catch {
    return defaultSimulationProfile;
  }
}

function storeSimulationProfile(value: StoredSimulationProfile) {
  try {
    // Large GLB data URLs are governed by the real settings backend. The
    // browser cache keeps only small display metadata so quota failure cannot
    // discard a backend-persisted Face or Body selection.
    const browserSafeValue = {
      ...value,
      characterFaceGlbDataUrl: "",
      characterBodyGlbDataUrl: "",
    };
    localStorage.setItem(T023_SIMULATION_PROFILE_STORAGE_V1, JSON.stringify(browserSafeValue));
    return true;
  } catch {
    return false;
  }
}

type CharacterGlbSlot = "face" | "body";

type CharacterGlbRecord = {
  dataUrl: string;
  name: string;
  sha256: string;
};

const characterGlbErrors = {
  face: {
    size: "CHARACTER_FACE_GLB_SIZE_OR_EXTENSION_INVALID",
    header: "CHARACTER_FACE_GLB_HEADER_INVALID",
    persistence: "CHARACTER_FACE_GLB_PERSISTENCE_UNAVAILABLE",
  },
  body: {
    size: "CHARACTER_BODY_GLB_SIZE_OR_EXTENSION_INVALID",
    header: "CHARACTER_BODY_GLB_HEADER_INVALID",
    persistence: "CHARACTER_BODY_GLB_PERSISTENCE_UNAVAILABLE",
  },
} as const;

function backendCharacterGlb(value: unknown): CharacterGlbRecord | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const dataUrl = typeof row.data_url === "string" && row.data_url.startsWith("data:") ? row.data_url : "";
  const name = typeof row.name === "string" && row.name.toLowerCase().endsWith(".glb") ? row.name : "";
  const sha256 = typeof row.sha256 === "string" && /^[a-f0-9]{64}$/i.test(row.sha256) ? row.sha256.toLowerCase() : "";
  return dataUrl && name && sha256 ? { dataUrl, name, sha256 } : null;
}

function resultRecord(receipt: SimulationReceipt): Record<string, unknown> {
  return receipt.result && typeof receipt.result === "object" && !Array.isArray(receipt.result)
    ? receipt.result as Record<string, unknown>
    : {};
}

function backendWorkspaceRoots(value: unknown): WorkspaceRootView[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const result = value as Record<string, unknown>;
  const nested = result.workspace_roots && typeof result.workspace_roots === "object" && !Array.isArray(result.workspace_roots)
    ? result.workspace_roots as Record<string, unknown>
    : result;
  const rows = Array.isArray(nested.roots) ? nested.roots : [];
  return rows.flatMap((entry) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) return [];
    const row = entry as Record<string, unknown>;
    const path = String(row.path || "").trim();
    if (!path) return [];
    return [{
      path,
      displayName: String(row.display_name || "").trim() || path.split(/[\\/]/).filter(Boolean).at(-1) || "EvidenceOS root",
      active: row.active === true,
      source: String(row.source || ""),
    }];
  });
}

function backendVersions(receipt: SimulationReceipt): BrainVersionView[] {
  const versions = resultRecord(receipt).versions;
  if (!Array.isArray(versions)) return [];
  return versions.flatMap((value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const row = value as Record<string, unknown>;
    const id = String(row.version_id || "");
    const signature = String(row.snapshot_hash || row.record_sha256 || "");
    if (!id || !/^[a-f0-9]{64}$/i.test(signature)) return [];
    return [{
      id,
      schemaVersion: "immutable",
      time: String(row.timestamp_utc || "verified timestamp unavailable"),
      signature: signature.toUpperCase(),
      summary: String(row.change_summary || row.reason || "Verified immutable brain version"),
    }];
  });
}

function outputFolderName(name: string) {
  return name.replace(/[<>:"/\\|?*]/g, "_").trim() || "brain";
}

function captureBrainOutputRoute(root: string, name: string) {
  return `${root.replace(/[\\/]+$/, "")}\\${outputFolderName(name)}`;
}

const initialBrainOutputRoutes = {} as Record<string, string>;
const dialogFocusableSelector = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

type BrainVersionView = { id: string; schemaVersion: string; time: string; signature: string; summary: string };

type BackendPipelineEvent = Partial<UniversalProcessTaskEvent>;

type ActiveNativeBuildRoute = {
  brainName: string;
  displayName: string;
  pipelineId: string;
  processPid: number;
  requestId: string;
  taskId: string;
  startedAtMs: number;
  workspaceDir: string;
};

type BackendPipelineStage = {
  stage_id?: string;
  stage_name?: string;
  stage_order?: number;
  status?: string;
};

const backendPipelineStages: BackendPipelineStage[] = [
  { stage_id: "source_validation_registration", stage_name: "Source validation and registration", stage_order: 1 },
  { stage_id: "sqlite_project_router_sector_creation", stage_name: "SQLite project/router/sector creation", stage_order: 2 },
  { stage_id: "per_lane_parsing_chunking_indexing", stage_name: "Per-lane parsing/chunking/indexing", stage_order: 3 },
  { stage_id: "pointer_router_hash_finalization", stage_name: "Pointer/router/hash finalization", stage_order: 4 },
  { stage_id: "project_mmd_generation", stage_name: "Project MMD generation", stage_order: 5 },
  { stage_id: "svg_png_rendering", stage_name: "SVG/PNG rendering", stage_order: 6 },
  { stage_id: "chatgpt_package_compilation", stage_name: "ChatGPT package compilation", stage_order: 7 },
  { stage_id: "gemini_exact10_compilation", stage_name: "Gemini provider-readable package compilation", stage_order: 8 },
  { stage_id: "package_hash_validation", stage_name: "Package/hash validation", stage_order: 9 },
  { stage_id: "immutable_version_capture", stage_name: "Immutable version capture", stage_order: 10 },
];

type SimChat = { id: string; title: string; brain: string; pinned: boolean };
type WorkspaceRootView = { path: string; displayName: string; active: boolean; source: string };
type BrainCatalogView = {
  catalogId: string;
  displayName: string;
  brainName: string;
  workspaceDir: string;
  rootPath: string;
  rootDisplayName: string;
  activeRoot: boolean;
  legacyRoot: boolean;
  outputDir: string;
  pinned: boolean;
  versionCount: number;
  rollbackCount: number;
  currentVersionAt: string;
  updatedAt: string;
};
type BrainMenuAnchor = { brain: string; left: number; top: number };
type SelectedBrainSource = {
  source_id: string;
  lane_key: string;
  path?: string;
  text?: string;
  display_name?: string;
  source_type?: string;
  active?: boolean;
};
type SelectedBrainSimulationContext = {
  context_schema: "T023_SELECTED_BRAIN_CONTEXT_V1";
  context_sha256: string;
  generation_token: string;
  brain_identity: { brain_id: string; brain_name: string; brain_slug: string; output_dir: string };
  summary: Record<string, unknown>;
  sources: SelectedBrainSource[];
  lane_schemas: Record<string, { schema: string[]; version: number }>;
  lane_definitions: Record<string, Record<string, unknown>>;
  versions: { versions?: Array<Record<string, unknown>> };
  output_route: { output_dir?: string };
  packages: unknown[];
  refresh_attention: Record<string, unknown>;
  refresh_candidate: Record<string, unknown>;
  selection_authority: {
    schema: typeof t023UiSystemSchemas.selectedBrainAuthority;
    brain_id: string;
    brain_name: string;
    workspace_dir: string;
    output_dir: string;
    accepted_version_id: string;
    accepted_snapshot_hash: string;
    candidate_delta_id: string;
    candidate_status: string;
    context_mode: "READ_ONLY_ATOMIC_SELECTED_BRAIN";
  };
  selection_commit: {
    schema: typeof t023UiSystemSchemas.selectedBrainAuthority;
    selection_request_id: string;
    status: "RESTORED" | "COMMITTED";
    context_sha256: string;
    brain_id: string;
    brain_name: string;
    workspace_dir: string;
    display_name: string;
  };
  model_output_state: Record<string, unknown>;
  historical_provider_delta_archive: Record<string, unknown>;
  telemetry_bundle: PersistedTelemetryBundle;
  pipeline_state: {
    pipeline?: BackendPipelineEvent | null;
    task_event?: BackendPipelineEvent | null;
    history?: BackendPipelineEvent[];
    task_history?: BackendPipelineEvent[];
    task_event_source?: string;
    stages?: BackendPipelineStage[];
    restored_from_disk?: boolean;
  };
  context_reuse_contract: {
    mode: string;
    raw_sources_reparsed: false;
    raw_sources_rechunked: false;
    raw_source_hashes_recomputed: false;
    raw_sources_reindexed: false;
    version_payload_hashes_reverified: false;
  };
  selection_context_metrics?: {
    backend_context_ms?: number;
    version_hash_verification?: boolean;
    source_processing?: string;
  };
};

function backendBrainCatalog(value: unknown): BrainCatalogView[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const record = value as Record<string, unknown>;
  const catalog = record.brain_catalog && typeof record.brain_catalog === "object" && !Array.isArray(record.brain_catalog)
    ? record.brain_catalog as Record<string, unknown>
    : record;
  const rows = Array.isArray(catalog.brains) ? catalog.brains : [];
  return rows.flatMap((entry) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) return [];
    const row = entry as Record<string, unknown>;
    const catalogId = String(row.catalog_id || "").trim();
    const brainName = String(row.brain_name || "").trim();
    const displayName = String(row.display_name || brainName).trim();
    const workspaceDir = String(row.workspace_dir || "").trim();
    const outputDir = String(row.output_dir || "").trim();
    if (!catalogId || !brainName || !displayName || !workspaceDir || !outputDir) return [];
    return [{
      catalogId,
      displayName,
      brainName,
      workspaceDir,
      rootPath: String(row.root_path || workspaceDir),
      rootDisplayName: String(row.root_display_name || "EvidenceOS root"),
      activeRoot: row.active_root === true,
      legacyRoot: row.legacy_root === true,
      outputDir,
      pinned: row.pinned === true,
      versionCount: Number(row.version_count || 0),
      rollbackCount: Number(row.rollback_count || 0),
      currentVersionAt: String(row.current_version_at || ""),
      updatedAt: String(row.updated_at || ""),
    }];
  });
}

function formatPipelineDuration(value: unknown) {
  const total = Math.max(0, Math.round(Number(value) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours > 0
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function pipelineRoutePathKey(value: unknown) {
  return String(value || "")
    .trim()
    .replaceAll("/", "\\")
    .replace(/\\+$/, "")
    .toLocaleLowerCase();
}

function runtimeTaskEventKey(event: BackendPipelineEvent) {
  const identity = universalTaskSurfaceIdentity(event);
  return [
    identity?.taskId || "",
    event.update_timestamp || event.updated_at || "",
    event.stage_id || "",
    event.tool_id || "",
    event.status || "",
    event.global_percent ?? event.progress ?? "",
  ].join("|");
}

function withPersistedRuntimeTaskEvent(
  context: SelectedBrainSimulationContext,
  taskEvent: BackendPipelineEvent,
) {
  const history = new Map<string, BackendPipelineEvent>();
  for (const event of [
    ...(context.pipeline_state?.task_history || []),
    taskEvent,
  ]) history.set(runtimeTaskEventKey(event), event);
  return {
    ...context,
    pipeline_state: {
      ...context.pipeline_state,
      task_event: taskEvent,
      task_history: Array.from(history.values()).slice(-512),
      task_event_source: "LIVE_NATIVE_EVENT_STREAM_PERSISTED_PER_BRAIN",
      restored_from_disk: true,
    },
  };
}
function contextVersionViews(context: SelectedBrainSimulationContext): BrainVersionView[] {
  return (context.versions.versions || []).flatMap((row) => {
    const id = String(row.version_id || "");
    const signature = String(row.snapshot_hash || row.record_sha256 || "");
    if (!id || !/^[a-f0-9]{64}$/i.test(signature)) return [];
    return [{
      id,
      schemaVersion: "immutable",
      time: String(row.timestamp_utc || "verified timestamp unavailable"),
      signature: signature.toUpperCase(),
      summary: String(row.change_summary || row.reason || "Verified immutable brain version"),
    }];
  });
}

const brainPalette = ["#efca72", "#82d9ec", "#78d6b0", "#a9a0ee", "#f29ab0", "#69d9f5", "#f1a36f", "#9ed47c", "#d29fe8", "#6fb4f2", "#ed8f9a", "#b0d96b"];
const initialBrainColors = Object.fromEntries(initialBrains.map((name, index) => [name, brainPalette[index]])) as Record<string, string>;

function brainColor(name: string) {
  const hash = Array.from(name).reduce((value, character) => value + character.charCodeAt(0), 0);
  return brainPalette[hash % brainPalette.length];
}

function nextUnusedBrainColor(usedColors: string[]) {
  const normalized = new Set(usedColors.map((color) => color.toLowerCase()));
  const paletteColor = brainPalette.find((color) => !normalized.has(color.toLowerCase()));
  if (paletteColor) return paletteColor;
  const hue = Math.round((usedColors.length * 137.508 + 38) % 360);
  return `hsl(${hue} 69% 67%)`;
}

function BrainOrb({ brain, size = 34, color }: { brain: string; size?: number; color?: string }) {
  return <BrainGlassSphere size={size} color={color || brainColor(brain)} className="cockpit-brain-orb" />;
}

function BrainSignal({ brain, color }: { brain: string; color?: string }) {
  const signalColor = color || brainColor(brain);
  return <svg className="simulation-search-wave" viewBox="0 0 112 28" preserveAspectRatio="none" style={{ "--result-color": signalColor } as CSSProperties} aria-hidden="true"><path d="M1 14h18l4-5 5 12 6-19 7 23 7-16 6 8 6-3h51" /></svg>;
}

function VersionIdentity({ brain, version, color, onCopy }: { brain: string; version: BrainVersionView; color: string; onCopy: () => void }) {
  return <>
    <span className="simulation-version-brain-pill">
      <BrainOrb brain={brain} size={62} color={color} />
      <span className="simulation-version-brain-copy"><strong>{brain}</strong><small>v{version.schemaVersion}</small></span>
      <BrainSignal brain={brain} color={color} />
      <i className="simulation-version-state" style={{ "--result-color": color } as CSSProperties} />
    </span>
    <span className="simulation-version-identity">
      <strong>{version.summary}</strong>
      <small>v{version.schemaVersion} · {version.time}</small>
      <button type="button" className="simulation-version-hash" title={`Copy SHA-256 ${version.signature}`} onClick={(event) => { event.stopPropagation(); onCopy(); }}>SHA-256 · {version.signature}</button>
    </span>
  </>;
}

function popupTitle(popup: Exclude<EvidencePopup, null>) {
  return {
    "new-brain": "New Brain",
    search: "Search / Explorer",
    pinned: "Pinned Items",
    "source-intake": "Source Intake",
    profile: "Profile",
    settings: "Settings",
    admin: "Admin Cockpit",
    "brain-output": "Brain Output",
    "output-directory": "Output Directory",
    "codex-handoff": "Codex Brain Handoff",
    ollama: "Ollama Launcher",
    refresh: "Refresh Brain",
    fuse: "Fuse Candidate",
    flash: "Flash Prompt",
    "plan-goal": "Canonical Plan Goal",
    "version-control": "Brain Version Control",
    "task-details": "System Task Details",
    "rename-brain": "Rename Brain",
    "delete-brain": "Delete Brain to Recycle Bin",
  }[popup];
}

export function EvidenceOSCockpitSimulation() {
  const reduceMotion = useReducedMotion();
  const storedProfile = useMemo(() => loadSimulationProfile(), []);
  const [activeModule, setActiveModule] = useState<"sqlite" | "telemetry3d">("sqlite");
  const [collapsed, setCollapsed] = useState(false);
  const [event, setEvent] = useState("Ready for native Evidence Lane operation");
  const [phase, setPhase] = useState<ToolFlowPhase>("idle");
  const [taskRequestPending, setTaskRequestPending] = useState(false);
  const [motionSubject, setMotionSubject] = useState<ToolFlowSubject>("tool");
  const [brainMotionState, setBrainMotionState] = useState<BrainMotionState>("idle");
  const [motionReceiptReady, setMotionReceiptReady] = useState(false);
  const [refreshReady, setRefreshReady] = useState(false);
  const [refreshCandidateId, setRefreshCandidateId] = useState<string | null>(null);
  const [refreshComplete, setRefreshComplete] = useState(false);
  const [fuseComplete, setFuseComplete] = useState(false);
  const [paused, setPaused] = useState(false);
  const [toolIndex, setToolIndex] = useState(0);
  const [progress, setProgress] = useState(0);
  const [brains, setBrains] = useState(initialBrains);
  const [brainCatalog, setBrainCatalog] = useState<BrainCatalogView[]>([]);
  const brainCatalogByDisplayRef = useRef<Record<string, BrainCatalogView>>({});
  const activeWorkspaceRef = useRef("");
  const [brainColors, setBrainColors] = useState<Record<string, string>>(initialBrainColors);
  const [defaultOutputRoot, setDefaultOutputRoot] = useState(initialDefaultOutputRoot);
  const [outputRootResult, setOutputRootResult] = useState("");
  const [workspaceRoots, setWorkspaceRoots] = useState<WorkspaceRootView[]>([]);
  const [brainOutputRoutes, setBrainOutputRoutes] = useState<Record<string, string>>(initialBrainOutputRoutes);
  const [selectedBrain, setSelectedBrain] = useState(initialBrains[0]);
  const [committedBrain, setCommittedBrain] = useState("");
  const [selectionAuthorityMode, setSelectionAuthorityMode] = useState<SelectedBrainAuthorityMode>("UNBOUND");
  const [selectedBrainContext, setSelectedBrainContext] = useState<SelectedBrainSimulationContext | null>(null);
  const selectedBrainContextCacheRef = useRef<Record<string, SelectedBrainSimulationContext>>({});
  const [livePipeline, setLivePipeline] = useState<BackendPipelineEvent | null>(null);
  const [liveTaskHistory, setLiveTaskHistory] = useState<BackendPipelineEvent[]>([]);
  const [showRestoredPipeline, setShowRestoredPipeline] = useState(false);
  const activeNativeBuildRouteRef = useRef<ActiveNativeBuildRoute | null>(null);
  const taskPollStartedAtRef = useRef(0);
  const pipelineMotionKeyRef = useRef("");
  const pipelineStageReceiptsRef = useRef<Set<number>>(new Set());
  const toolIndexRef = useRef(0);
  const [pipelineTerminalStatus, setPipelineTerminalStatus] = useState<"" | "completed" | "failed" | "cancelled">("");
  const [pipelineClock, setPipelineClock] = useState(() => Date.now());
  const brainSelectionGenerationRef = useRef(0);
  const sourcePickerInFlightRef = useRef(false);
  const [pinnedBrains, setPinnedBrains] = useState<string[]>([initialBrains[0]]);
  const [brainMenu, setBrainMenu] = useState<BrainMenuAnchor | null>(null);
  const [editingBrain, setEditingBrain] = useState<string | null>(null);
  const [actionBrain, setActionBrain] = useState("");
  const [popup, setPopup] = useState<EvidencePopup>(null);
  const [activeLaneKey, setActiveLaneKey] = useState("");
  const [customLanes, setCustomLanes] = useState<EvidenceLane[]>([]);
  const [laneSchemas, setLaneSchemas] = useState<Record<string, string[]>>(() => Object.fromEntries(evidenceSourceLanes.map((lane) => [lane.key, lane.schema])));
  const [laneSchemaVersions, setLaneSchemaVersions] = useState<Record<string, number>>({});
  const [schemaInspectorLaneKey, setSchemaInspectorLaneKey] = useState("");
  const [schemaInspectorDraft, setSchemaInspectorDraft] = useState("");
  const [schemaInspectorError, setSchemaInspectorError] = useState("");
  const [schemaInspectorBusy, setSchemaInspectorBusy] = useState(false);
  const [laneDrafts, setLaneDrafts] = useState<Record<string, Record<string, string>>>({});
  const [validatedLanes, setValidatedLanes] = useState<string[]>([]);
  const [newBrainName, setNewBrainName] = useState("");
  const [newBrainPreviewColor, setNewBrainPreviewColor] = useState(() => nextUnusedBrainColor(Object.values(initialBrainColors)));
  const [newBrainStage, setNewBrainStage] = useState<"idle" | "success" | "settled">("idle");
  const newBrainTimers = useRef<number[]>([]);
  const popupDialogRef = useRef<HTMLDivElement>(null);
  const popupReturnFocusRef = useRef<HTMLElement | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [profileName, setProfileName] = useState(storedProfile.profileName);
  const [profileRole, setProfileRole] = useState(storedProfile.profileRole);
  const [profileImageUrl, setProfileImageUrl] = useState(storedProfile.profileImageDataUrl);
  const [profileImageName, setProfileImageName] = useState(storedProfile.profileImageName);
  const [characterFaceGlbDataUrl, setCharacterFaceGlbDataUrl] = useState(storedProfile.characterFaceGlbDataUrl);
  const [characterFaceGlbName, setCharacterFaceGlbName] = useState(storedProfile.characterFaceGlbName);
  const [characterFaceGlbSha256, setCharacterFaceGlbSha256] = useState(storedProfile.characterFaceGlbSha256);
  const [characterBodyGlbDataUrl, setCharacterBodyGlbDataUrl] = useState(storedProfile.characterBodyGlbDataUrl);
  const [characterBodyGlbName, setCharacterBodyGlbName] = useState(storedProfile.characterBodyGlbName);
  const [characterBodyGlbSha256, setCharacterBodyGlbSha256] = useState(storedProfile.characterBodyGlbSha256);
  const [profileEditing, setProfileEditing] = useState(false);
  const [appearance, setAppearance] = useState("system");
  const [accent, setAccent] = useState("evidence");
  const [popupReceipt, setPopupReceipt] = useState<SimulationReceipt | null>(null);
  const [versions, setVersions] = useState<BrainVersionView[]>([]);
  const [selectedVersion, setSelectedVersion] = useState("");
  const [rollbackResult, setRollbackResult] = useState("");
  const [versionDropArmed, setVersionDropArmed] = useState(false);
  const [versionDropConfirmation, setVersionDropConfirmation] = useState("");
  const [versionDropResult, setVersionDropResult] = useState("");
  const [flashPrompt, setFlashPrompt] = useState("");
  const [planGoalPrompt, setPlanGoalPrompt] = useState("");
  const [planDeltaState, setPlanDeltaState] = useState<Record<string, unknown> | null>(null);
  const [selectedPlanDelta, setSelectedPlanDelta] = useState("");
  const [planSteerDraft, setPlanSteerDraft] = useState("");
  const [planDeltaActionResult, setPlanDeltaActionResult] = useState("");
  const [ollamaExecutablePath, setOllamaExecutablePath] = useState("");
  const [receipts, setReceipts] = useState<SimulationReceipt[]>([]);
  const [chats, setChats] = useState<SimChat[]>([
    { id: "chat-001", title: "Backend correction", brain: initialBrains[0], pinned: true },
    { id: "chat-002", title: "UI theme integration", brain: initialBrains[1], pinned: false },
  ]);
  const [selectedChatId, setSelectedChatId] = useState("chat-001");
  const building = taskRequestPending || !["idle", "complete", "failed"].includes(phase);
  useEffect(() => {
    toolIndexRef.current = toolIndex;
  }, [toolIndex]);
  const availableSourceLanes = useMemo(() => [...evidenceSourceLanes, ...customLanes].map((lane) => ({ ...lane, schema: laneSchemas[lane.key] || lane.schema })), [customLanes, laneSchemas]);
  const activeLane = availableSourceLanes.find((lane) => lane.key === activeLaneKey);
  const activeLanePolicy = activeLane ? evidenceLanePickerPolicy[activeLane.key] || evidenceLanePickerPolicy.custom : null;
  const activeLaneDraft = activeLane ? (laneDrafts[activeLane.key] || {}) : {};
  const planDeltaRows = useMemo(() => {
    const rows = planDeltaState?.deltas;
    return Array.isArray(rows)
      ? rows.filter((row): row is Record<string, unknown> => Boolean(row && typeof row === "object" && !Array.isArray(row)))
      : [];
  }, [planDeltaState]);
  const contextSourcesByLane = useMemo(() => {
    const grouped: Record<string, string[]> = {};
    for (const source of selectedBrainContext?.sources || []) {
      const laneKey = String(source.lane_key || "custom");
      const value = String(source.path || source.text || "").trim();
      if (!value) continue;
      grouped[laneKey] = [...(grouped[laneKey] || []), value];
    }
    return grouped;
  }, [selectedBrainContext]);
  const colorFor = (name: string) => brainColors[name] || brainColor(name);
  const brainCatalogEntry = (name: string) => brainCatalogByDisplayRef.current[name];
  const governedBrainRoute = (displayName: string = selectedBrain) => {
    const contextBrainName = String(selectedBrainContext?.brain_identity?.brain_name || "").trim();
    const contextOutputDir = String(
      selectedBrainContext?.output_route?.output_dir
      || selectedBrainContext?.brain_identity?.output_dir
      || "",
    ).trim();
    const entry = brainCatalogEntry(displayName)
      || brainCatalog.find((candidate) => candidate.displayName === displayName)
      || brainCatalog.find((candidate) => (
        Boolean(contextBrainName)
        && candidate.brainName === contextBrainName
        && (!contextOutputDir || candidate.outputDir === contextOutputDir)
      ));
    return entry
      ? { brain_name: entry.brainName, workspace_dir: entry.workspaceDir }
      : { brain_name: contextBrainName || displayName, workspace_dir: activeWorkspaceRef.current || "" };
  };
  const brainRailMeta = (name: string) => {
    const entry = brainCatalogEntry(name);
    if (!entry) return name === committedBrain ? "active session" : name === selectedBrain ? selectionAuthorityMode === "PENDING" ? "loading saved state" : "viewing · backend unbound" : "topology ready";
    const rawDate = entry.currentVersionAt || entry.updatedAt;
    const date = rawDate && !Number.isNaN(Date.parse(rawDate)) ? new Date(rawDate).toLocaleDateString() : "unversioned";
    return `${name === committedBrain ? "active" : name === selectedBrain ? selectionAuthorityMode === "PENDING" ? "loading saved state" : "viewing" : entry.legacyRoot ? "legacy" : "current"} · ${entry.rootDisplayName} · ${entry.rollbackCount} rollback · ${date}`;
  };
  const profileAvatar = (size: number) => <GlassIconOrb className="cockpit-profile-avatar" color="#82d9ec" size={size} label={`${profileName} profile`}>
    {profileImageUrl ? <img src={profileImageUrl} alt="" /> : "PR"}
  </GlassIconOrb>;

  const adapter = useMemo(() => createNativeEvidenceAdapter((receipt) => {
    setReceipts((current) => [receipt, ...current].slice(0, 40));
  }), []);

  function commitBrainCatalog(value: unknown) {
    const catalog = backendBrainCatalog(value);
    const byDisplay = Object.fromEntries(catalog.map((entry) => [entry.displayName, entry]));
    brainCatalogByDisplayRef.current = byDisplay;
    setBrainCatalog(catalog);
    setBrains(catalog.map((entry) => entry.displayName));
    setBrainOutputRoutes(Object.fromEntries(catalog.map((entry) => [entry.displayName, entry.outputDir])));
    setPinnedBrains(catalog.filter((entry) => entry.pinned).map((entry) => entry.displayName));
    setBrainColors((current) => ({
      ...current,
      ...Object.fromEntries(catalog.map((entry) => [entry.displayName, current[entry.displayName] || brainColor(entry.catalogId)])),
    }));
    return catalog;
  }

  async function execute(command: EvidenceUiCommand, label: string, payload: Record<string, unknown> = {}) {
    const routedPayload = { ...payload };
    const requestedBrain = String(routedPayload.brain_name || routedPayload.brainName || "").trim();
    const catalogEntry = requestedBrain ? brainCatalogByDisplayRef.current[requestedBrain] : undefined;
    if (catalogEntry) {
      if (command === "brain.select") {
        if (routedPayload.selection_restore_only !== true) {
          routedPayload.selection_display_name = requestedBrain;
          if (activeWorkspaceRef.current) {
            routedPayload.selection_persistence_workspace_dir = activeWorkspaceRef.current;
          }
        }
      }
      routedPayload.brain_name = catalogEntry.brainName;
      routedPayload.workspace_dir = catalogEntry.workspaceDir;
      if (String(routedPayload.confirm_brain_name || "").trim() === requestedBrain) {
        routedPayload.confirm_brain_name = catalogEntry.brainName;
      }
      delete routedPayload.brainName;
    } else if (!routedPayload.workspace_dir && !routedPayload.workspaceDir && activeWorkspaceRef.current) {
      routedPayload.workspace_dir = activeWorkspaceRef.current;
    }
    const receipt = await adapter.execute(command, { ...routedPayload, label });
    const route = receipt.mode === "backend"
      ? receipt.backend_connected ? "REAL BACKEND" : "BACKEND FAILED CLOSED"
      : "UI ONLY";
    const failureDetail = receipt.ok ? "" : ` · ${receipt.error || "NATIVE_BACKEND_COMMAND_FAILED"}`;
    setEvent(`${label} · ${receipt.id} · ${route}${failureDetail}`);
    return receipt;
  }

  useEffect(() => {
    const onWorkerEvent = (rawEvent: Event) => {
      const detail = (rawEvent as CustomEvent<Record<string, unknown>>).detail;
      const workerEventName = String(detail?.event || "");
      if (!detail || !["task.started", "task.progress", "task.done", "task.error"].includes(workerEventName)) return;
      const rawPipeline = detail.payload;
      if (!rawPipeline || typeof rawPipeline !== "object" || Array.isArray(rawPipeline)) return;
      const pipeline = rawPipeline as BackendPipelineEvent;
      const taskIdentity = universalTaskSurfaceIdentity(pipeline);
      if (!taskIdentity) return;
      setTaskRequestPending(false);
      const pipelineBrain = String(pipeline.brain_name || "").trim();
      const pipelineWorkspace = pipelineRoutePathKey(pipeline.workspace_dir);
      const pipelineId = String(pipeline.pipeline_id || "").trim();
      const requestId = String(pipeline.request_id || detail.request_id || "").trim();
      const taskId = taskIdentity.taskId;
      const activeRoute = activeNativeBuildRouteRef.current;
      if (activeRoute) {
        const expectedBrains = new Set([activeRoute.brainName, activeRoute.displayName].filter(Boolean));
        if (pipelineBrain && !expectedBrains.has(pipelineBrain)) return;
        const expectedWorkspace = pipelineRoutePathKey(activeRoute.workspaceDir);
        if (pipelineWorkspace && expectedWorkspace && pipelineWorkspace !== expectedWorkspace) return;
        if (activeRoute.pipelineId && pipelineId && pipelineId !== activeRoute.pipelineId) return;
        if (activeRoute.requestId && requestId && requestId !== activeRoute.requestId) return;
        if (activeRoute.taskId && taskId !== activeRoute.taskId) return;
        activeNativeBuildRouteRef.current = {
          ...activeRoute,
          pipelineId: activeRoute.pipelineId || pipelineId,
          processPid: Math.max(activeRoute.processPid, Number(pipeline.process_pid) || 0),
          requestId: activeRoute.requestId || requestId,
          taskId: activeRoute.taskId || taskId,
        };
      } else {
        const catalogRoute = brainCatalogEntry(selectedBrain);
        const expectedBrain = String(selectedBrainContext?.brain_identity?.brain_name || catalogRoute?.brainName || selectedBrain);
        if (pipelineBrain && pipelineBrain !== expectedBrain && pipelineBrain !== selectedBrain) return;
        const expectedWorkspace = pipelineRoutePathKey(catalogRoute?.workspaceDir || activeWorkspaceRef.current);
        if (pipelineWorkspace && expectedWorkspace && pipelineWorkspace !== expectedWorkspace) return;
      }
      const status = String(pipeline.status || "running").toLowerCase();
      const command = String(pipeline.command || "");
      const eventMotionSubject: ToolFlowSubject = command === "brain.refresh.fuse"
        ? "fuse"
        : command.startsWith("brain.refresh.")
          ? "refresh"
          : "tool";
      const isBuildPipeline = command === "brain.build" || command === "brain.buildAll";
      const stageOrder = Math.max(1, Number(pipeline.stage_order) || 1);
      const stageCount = Math.max(1, Number(pipeline.stage_count) || (isBuildPipeline ? evidenceToolFlow.length : 1));
      const nextProgress = Math.max(0, Math.min(100, Number(pipeline.global_percent ?? pipeline.progress) || 0));
      const catalogDisplayName = Object.values(brainCatalogByDisplayRef.current)
        .find((entry) => entry.brainName === pipelineBrain)?.displayName;
      const routedDisplayName = activeRoute?.displayName || catalogDisplayName || selectedBrain;
      const cacheAliases = new Set([routedDisplayName, pipelineBrain].filter(Boolean));
      for (const alias of cacheAliases) {
        const cachedContext = selectedBrainContextCacheRef.current[alias];
        if (cachedContext) {
          selectedBrainContextCacheRef.current[alias] = withPersistedRuntimeTaskEvent(cachedContext, pipeline);
        }
      }
      setSelectedBrainContext((current) => {
        if (!current) return current;
        const currentBrainName = String(current.brain_identity?.brain_name || "");
        if (routedDisplayName !== selectedBrain && pipelineBrain !== currentBrainName) return current;
        const nextContext = withPersistedRuntimeTaskEvent(current, pipeline);
        selectedBrainContextCacheRef.current[routedDisplayName] = nextContext;
        if (pipelineBrain) selectedBrainContextCacheRef.current[pipelineBrain] = nextContext;
        return nextContext;
      });
      const liveStage = String(pipeline.stage_name || pipeline.stage_id || "command_start");
      setEvent(`${command || "Task"} · ${liveStage} · ${Math.round(nextProgress)}% · REAL BACKEND LIVE`);
      if (isBuildPipeline && stageCount !== evidenceToolFlow.length) {
        setMotionReceiptReady(false);
        setPipelineTerminalStatus("failed");
        setPhase("failed");
        setEvent(`Build failed closed · expected ${evidenceToolFlow.length} canonical stages, received ${stageCount}`);
        return;
      }
      const motionKey = taskId || requestId || pipelineId || `${pipelineBrain}:${String(pipeline.started_at || pipeline.start_timestamp || "")}`;
      const newMotion = Boolean(motionKey && pipelineMotionKeyRef.current !== motionKey);
      const motionEligible = eventMotionSubject !== "tool"
        ? status === "running"
        : isBuildPipeline && status === "running" && String(pipeline.stage_id || "") !== "command_start";
      if (newMotion && motionEligible) {
        pipelineMotionKeyRef.current = motionKey;
        pipelineStageReceiptsRef.current = new Set();
        toolIndexRef.current = 0;
        setToolIndex(0);
        setPipelineTerminalStatus("");
        setMotionReceiptReady(false);
        setBrainMotionState(eventMotionSubject === "fuse" ? "fuse-grow-pulse" : "idle");
        setPhase("flying-to-lens");
      }
      const completedThrough = status === "completed"
        ? evidenceToolFlow.length
        : Math.max(0, stageOrder - 1 + (Number(pipeline.stage_percent) >= 100 ? 1 : 0));
      if (isBuildPipeline) {
        for (let order = 1; order <= Math.min(completedThrough, evidenceToolFlow.length); order += 1) {
          pipelineStageReceiptsRef.current.add(order);
        }
      }
      const activeMotionOrder = (newMotion ? 0 : toolIndexRef.current) + 1;
      setLiveTaskHistory((current) => {
        const previousIdentity = universalTaskSurfaceIdentity(current.at(-1) || null);
        const sameTask = previousIdentity?.taskId === taskIdentity.taskId;
        return [...(sameTask ? current : []), pipeline].slice(-256);
      });
      setLivePipeline(pipeline);
      setPipelineClock(Date.now());
      setShowRestoredPipeline(true);
      setMotionSubject(eventMotionSubject);
      setProgress((current) => newMotion ? nextProgress : Math.max(current, nextProgress));
      setPaused(false);
      setMotionReceiptReady(
        isBuildPipeline
          ? pipelineStageReceiptsRef.current.has(activeMotionOrder)
          : !["queued", "running"].includes(status),
      );
      if (status === "failed" || status === "cancelled") {
        setMotionReceiptReady(false);
        setPipelineTerminalStatus(status);
        setPhase("failed");
        setEvent(`${command || "Task"} failed closed · ${String(pipeline.error_code || pipeline.failure_code || "BACKEND_TASK_FAILED")}`);
      } else if (status === "completed" || status === "hil_waiting" || status === "skipped") {
        setPipelineTerminalStatus("completed");
      }
    };
    window.addEventListener("evidence-os-worker-event", onWorkerEvent);
    return () => window.removeEventListener("evidence-os-worker-event", onWorkerEvent);
  }, [selectedBrain, selectedBrainContext?.brain_identity?.brain_name]);

  useEffect(() => {
    const liveStatus = String(livePipeline?.status || "").toLowerCase();
    if (!taskRequestPending && !["queued", "running"].includes(liveStatus)) return;
    let cancelled = false;
    let timer = 0;
    const pollRuntimeTask = async () => {
      try {
        const nativeTransport = window.__EVIDENCE_OS_NATIVE_COMMAND_TRANSPORT__;
        if (!nativeTransport) return;
        const activeRoute = activeNativeBuildRouteRef.current;
        const catalogRoute = brainCatalogEntry(selectedBrain);
        const brainName = String(activeRoute?.brainName || catalogRoute?.brainName || selectedBrain).trim();
        const workspaceDir = String(activeRoute?.workspaceDir || catalogRoute?.workspaceDir || activeWorkspaceRef.current).trim();
        const envelope = await nativeTransport("runtime.snapshot", {
          brain_name: brainName,
          workspace_dir: workspaceDir,
        }) as { ok?: boolean; result?: { task_event?: BackendPipelineEvent | null } };
        const taskEvent = envelope?.ok ? envelope.result?.task_event : null;
        if (!cancelled && taskEvent) {
          const eventStartedAt = Date.parse(String(taskEvent.started_at || taskEvent.start_timestamp || ""));
          const submittedAt = taskPollStartedAtRef.current;
          if (!submittedAt || !Number.isFinite(eventStartedAt) || eventStartedAt + 1_500 >= submittedAt) {
            const status = String(taskEvent.status || "running").toLowerCase();
            const workerEventName = ["failed", "cancelled"].includes(status)
              ? "task.error"
              : ["completed", "hil_waiting", "skipped"].includes(status)
                ? "task.done"
                : "task.progress";
            window.dispatchEvent(new CustomEvent("evidence-os-worker-event", {
              detail: {
                type: "event",
                event: workerEventName,
                request_id: taskEvent.request_id,
                payload: taskEvent,
                source: "NATIVE_RUNTIME_SNAPSHOT_POLL",
              },
            }));
          }
        }
      } finally {
        if (!cancelled) timer = window.setTimeout(pollRuntimeTask, 300);
      }
    };
    void pollRuntimeTask();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [taskRequestPending, livePipeline?.status, selectedBrain]);

  useEffect(() => {
    const status = String(livePipeline?.status || "").toLowerCase();
    if (!livePipeline || ["completed", "failed", "cancelled"].includes(status)) return;
    setPipelineClock(Date.now());
    const timer = window.setInterval(() => setPipelineClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [livePipeline?.pipeline_id, livePipeline?.request_id, livePipeline?.started_at, livePipeline?.status]);

  useEffect(() => {
    let active = true;
    void (async () => {
      let restoredBrainFromWorkspace = "";
      const workspaceReceipt = await execute("workspace.init", "Current output root catalog restored; legacy route pointers removed without deleting files");
      if (!active) return;
      if (workspaceReceipt.ok) {
        const workspace = resultRecord(workspaceReceipt);
        const restoredRoot = String(workspace.workspace_dir || "").trim();
        if (restoredRoot) {
          activeWorkspaceRef.current = restoredRoot;
          setDefaultOutputRoot(restoredRoot);
        }
        restoredBrainFromWorkspace = String(workspace.last_selected_brain || "").trim();
        setWorkspaceRoots(backendWorkspaceRoots(workspace));
        const catalog = commitBrainCatalog(workspace);
        const restoredEntry = catalog.find((entry) => entry.displayName === restoredBrainFromWorkspace)
          || catalog.find((entry) => entry.activeRoot && entry.brainName === restoredBrainFromWorkspace)
          || catalog.find((entry) => entry.displayName === selectedBrain)
          || catalog[0];
        const restoredBrain = restoredEntry?.displayName || "";
        setSelectedBrain(restoredBrain);
        if (restoredBrain) void selectBrain(restoredBrain, true, true, false);
        else setSelectedBrainContext(null);
      }

      const profileReceipt = await execute("identity.get", "Persistent local profile loaded from the real backend");
      if (!active) return;
      if (profileReceipt.ok) {
        const profile = resultRecord(profileReceipt);
        const displayName = String(profile.display_name || "").trim();
        const roleLabel = String(profile.role_label || "").trim();
        if (displayName) setProfileName(displayName);
        if (roleLabel) setProfileRole(roleLabel);
      }

      const settingsReceipt = await execute("settings.get", "Persistent Face and Body GLB settings loaded");
      if (!active) return;
      if (settingsReceipt.ok) {
        const categories = resultRecord(settingsReceipt).categories;
        const interfaceSettings = categories && typeof categories === "object" && !Array.isArray(categories)
          ? (categories as Record<string, unknown>).interface
          : null;
        const settings = interfaceSettings && typeof interfaceSettings === "object" && !Array.isArray(interfaceSettings)
          ? interfaceSettings as Record<string, unknown>
          : {};
        const face = backendCharacterGlb(settings.character_face_glb);
        const body = backendCharacterGlb(settings.character_body_glb);
        const persisted = { ...loadSimulationProfile() };
        if (face) {
          persisted.characterFaceGlbDataUrl = face.dataUrl;
          persisted.characterFaceGlbName = face.name;
          persisted.characterFaceGlbSha256 = face.sha256;
          setCharacterFaceGlbDataUrl(face.dataUrl);
          setCharacterFaceGlbName(face.name);
          setCharacterFaceGlbSha256(face.sha256);
        }
        if (body) {
          persisted.characterBodyGlbDataUrl = body.dataUrl;
          persisted.characterBodyGlbName = body.name;
          persisted.characterBodyGlbSha256 = body.sha256;
          setCharacterBodyGlbDataUrl(body.dataUrl);
          setCharacterBodyGlbName(body.name);
          setCharacterBodyGlbSha256(body.sha256);
        }
        if (face || body) storeSimulationProfile(persisted);
      }

      const ollamaReceipt = await execute("ollama.settings.get", "Persistent Ollama launch settings loaded");
      if (!active) return;
      if (ollamaReceipt.ok) {
        setOllamaExecutablePath(String(resultRecord(ollamaReceipt).ollama_executable_override || ""));
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  function persistSimulationProfileState(values: Partial<StoredSimulationProfile>) {
    return storeSimulationProfile({
      profileName,
      profileRole,
      profileImageDataUrl: profileImageUrl,
      profileImageName,
      characterFaceGlbDataUrl,
      characterFaceGlbName,
      characterFaceGlbSha256,
      characterBodyGlbDataUrl,
      characterBodyGlbName,
      characterBodyGlbSha256,
      ...values,
    });
  }

  useEffect(() => {
    if (paused) return;
    if (phase === "scanning") {
      if (!motionReceiptReady) return; // Backend receipt owns validation completion.
      setPhase("validated");
      return;
    }
    const delay = toolLensPhaseDelayMs[phase];
    if (!delay) return;
    const timer = window.setTimeout(() => {
      if (phase === "digesting") {
        if (!motionReceiptReady) return;
        setBrainMotionState("success-flash");
        if (motionSubject === "refresh") {
          setPhase("complete");
          setProgress(100);
          setEvent(refreshReady ? "Refresh candidate validated by the real backend; Fuse remains governed by the M0 HIL gate" : "Refresh completed with no candidate requiring Fuse");
        } else if (motionSubject === "fuse") {
          setPhase("complete");
          setProgress(100);
          setEvent("Explicit Fuse completed from a successful real backend receipt");
        } else if (toolIndex === evidenceToolFlow.length - 1) {
          if (pipelineTerminalStatus !== "completed") return;
          setPhase("complete");
          setProgress(100);
          setShowRestoredPipeline(true);
          setEvent("Coded Toolchain completed from the real backend receipt in the green state");
        } else {
          const nextToolIndex = toolIndex + 1;
          toolIndexRef.current = nextToolIndex;
          setToolIndex(nextToolIndex);
          setMotionReceiptReady(pipelineStageReceiptsRef.current.has(nextToolIndex + 1));
          setPhase("flying-to-lens");
        }
      } else {
        setPhase(phaseNext[phase] ?? "idle");
      }
    }, delay);
    return () => window.clearTimeout(timer);
  }, [motionReceiptReady, motionSubject, paused, phase, pipelineTerminalStatus, refreshReady, toolIndex]);

  useEffect(() => {
    if (!brainMenu) return;
    const dismiss = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (target.closest('[data-testid="brain-actions-motion"]') || target.closest('[data-brain-menu-trigger="true"]')) return;
      setBrainMenu(null);
    };
    const dismissOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setBrainMenu(null);
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", dismissOnEscape);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", dismissOnEscape);
    };
  }, [brainMenu]);

  const popupOpen = popup !== null;
  useEffect(() => {
    if (popup !== "version-control" || versions.length < 2) return;
    // A selected current head is informational only: rollback and drop must
    // always target history.  Context restoration and version-list refreshes
    // can arrive after the popup opens, so make the newest historical record
    // the stable default without preventing a later explicit card selection.
    setSelectedVersion((current) => (
      current
      && current !== versions[0].id
      && versions.some((version) => version.id === current)
        ? current
        : versions[1].id
    ));
  }, [popup, versions]);

  useEffect(() => {
    if (!popupOpen) return;

    popupReturnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusableElements = () => {
      const dialog = popupDialogRef.current;
      if (!dialog) return [];
      return Array.from(dialog.querySelectorAll<HTMLElement>(dialogFocusableSelector)).filter((element) => (
        element.getClientRects().length > 0 && window.getComputedStyle(element).visibility !== "hidden"
      ));
    };
    const focusInside = (preferLast = false) => {
      const dialog = popupDialogRef.current;
      if (!dialog) return;
      const elements = focusableElements();
      const target = preferLast ? elements.at(-1) : elements[0];
      (target ?? dialog).focus({ preventScroll: true });
    };
    const initialFocusFrame = window.requestAnimationFrame(() => {
      const dialog = popupDialogRef.current;
      if (!dialog || dialog.contains(document.activeElement)) return;
      focusInside();
    });
    const containFocus = (event: FocusEvent) => {
      const dialog = popupDialogRef.current;
      if (!dialog || !(event.target instanceof Node) || dialog.contains(event.target)) return;
      focusInside();
    };
    const handleDialogKeys = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closePopup();
        return;
      }
      if (event.key !== "Tab") return;

      const dialog = popupDialogRef.current;
      if (!dialog) return;
      const elements = focusableElements();
      if (!elements.length) {
        event.preventDefault();
        dialog.focus({ preventScroll: true });
        return;
      }
      const first = elements[0];
      const last = elements[elements.length - 1];
      const active = document.activeElement;
      if (!dialog.contains(active)) {
        event.preventDefault();
        focusInside(event.shiftKey);
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus({ preventScroll: true });
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus({ preventScroll: true });
      }
    };

    document.addEventListener("focusin", containFocus, true);
    document.addEventListener("keydown", handleDialogKeys, true);
    return () => {
      window.cancelAnimationFrame(initialFocusFrame);
      document.removeEventListener("focusin", containFocus, true);
      document.removeEventListener("keydown", handleDialogKeys, true);
      const returnFocus = popupReturnFocusRef.current;
      popupReturnFocusRef.current = null;
      if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
    };
  }, [popupOpen]);

  useEffect(() => () => {
    newBrainTimers.current.forEach((timer) => window.clearTimeout(timer));
  }, []);

  function clearNewBrainTimers() {
    newBrainTimers.current.forEach((timer) => window.clearTimeout(timer));
    newBrainTimers.current = [];
  }

  function closePopup() {
    clearNewBrainTimers();
    setPopup(null);
    setActiveLaneKey("");
    setDeleteConfirmation("");
    setRollbackResult("");
    setProfileEditing(false);
    setNewBrainStage("idle");
    setOutputRootResult("");
    setPopupReceipt(null);
    setSchemaInspectorLaneKey("");
    setSchemaInspectorDraft("");
    setSchemaInspectorError("");
  }

  function openPopup(next: Exclude<EvidencePopup, null>, command?: EvidenceUiCommand, label?: string) {
    if (next === "new-brain") {
      clearNewBrainTimers();
      setNewBrainStage("idle");
      setNewBrainPreviewColor(nextUnusedBrainColor(Object.values(brainColors)));
    }
    if (next === "output-directory") {
      setOutputRootResult("");
    }
    setPopup(next);
    if (command && label) void execute(command, label);
  }

  async function createBrainFromPopup() {
    if (newBrainStage !== "idle") return;
    const name = newBrainName.trim() || `New Brain ${brains.length + 1}`;
    if (brains.includes(name)) {
      void execute("brains.list", `${name} creation blocked locally: stable brain identity already exists`);
      return;
    }
    const receipt = await execute("brain.create", `${name} created by the real backend`, { brain_name: name });
    if (!receipt.ok) return;
    const assignedColor = newBrainPreviewColor;
    setNewBrainStage("success");
    const settleTimer = window.setTimeout(async () => {
      setNewBrainStage("settled");
      const catalogReceipt = await execute("brains.catalog", `${name} added to the governed current-and-legacy brain catalog`);
      const catalog = catalogReceipt.ok ? commitBrainCatalog(resultRecord(catalogReceipt)) : [];
      const createdEntry = catalog.find((entry) => entry.brainName === name && entry.workspaceDir.toLowerCase() === activeWorkspaceRef.current.toLowerCase());
      const displayName = createdEntry?.displayName || name;
      setBrainColors((current) => ({ ...current, [displayName]: assignedColor }));
      if (!createdEntry) {
        setBrainOutputRoutes((current) => ({ ...current, [displayName]: captureBrainOutputRoute(defaultOutputRoot, name) }));
        setBrains((current) => [displayName, ...current]);
      }
      void selectBrain(displayName, true);
      setEvent(`${name} created with unique ${assignedColor} identity and selected · ${receipt.id} · REAL BACKEND`);
      const closeTimer = window.setTimeout(() => {
        setNewBrainName("");
        closePopup();
      }, reduceMotion ? 20 : 620);
      newBrainTimers.current.push(closeTimer);
    }, reduceMotion ? 20 : 520);
    newBrainTimers.current.push(settleTimer);
  }

  async function copyVersionHash(version: BrainVersionView) {
    try {
      await navigator.clipboard.writeText(version.signature);
    } catch {
      const copyField = document.createElement("textarea");
      copyField.value = version.signature;
      copyField.style.position = "fixed";
      copyField.style.opacity = "0";
      document.body.appendChild(copyField);
      copyField.select();
      document.execCommand("copy");
      copyField.remove();
    }
    setRollbackResult(`SHA-256 copied · ${version.signature.slice(0, 12)}…`);
    execute("brain.version.copyHash", `${version.id} SHA-256 copied from the one-line signed version receipt`);
  }

  function sidebarAction(action: "add" | "search" | "pin") {
    if (action === "add") {
      openPopup("new-brain");
    }
    if (action === "search") {
      openPopup("search", "workspace.search", "Search opened from collapsed sidebar");
    }
    if (action === "pin") {
      openPopup("pinned", "session.update", "Pinned list opened from collapsed sidebar");
    }
  }

  function startReceiptBoundMotion(subject: ToolFlowSubject) {
    // The new governed command owns the live task surface immediately. Never
    // let a completed Build route filter the next Refresh/Fuse task envelope.
    activeNativeBuildRouteRef.current = null;
    pipelineMotionKeyRef.current = "";
    pipelineStageReceiptsRef.current = new Set();
    setPipelineTerminalStatus("");
    taskPollStartedAtRef.current = Date.now();
    setTaskRequestPending(true);
    setShowRestoredPipeline(false);
    setLivePipeline(null);
    setLiveTaskHistory([]);
    setMotionSubject(subject);
    setMotionReceiptReady(false);
    setBrainMotionState("idle");
    setPaused(false);
    toolIndexRef.current = 0;
    setToolIndex(0);
    setProgress(0);
    setPhase("idle");
    setEvent(`${subject} request submitted; waiting for the authoritative backend task event`);
  }

  function startNativeBuildMotion(route: { brain_name: string; workspace_dir?: string }) {
    const startedAtMs = Date.now();
    taskPollStartedAtRef.current = startedAtMs;
    setTaskRequestPending(true);
    setShowRestoredPipeline(false);
    setLivePipeline(null);
    setLiveTaskHistory([]);
    setMotionSubject("tool");
    setMotionReceiptReady(false);
    setBrainMotionState("idle");
    setPaused(false);
    toolIndexRef.current = 0;
    setToolIndex(0);
    setProgress(0);
    setPhase("idle");
    pipelineMotionKeyRef.current = "";
    pipelineStageReceiptsRef.current = new Set();
    setPipelineTerminalStatus("");
    const brainName = String(route.brain_name || selectedBrain).trim();
    const workspaceDir = String(route.workspace_dir || activeWorkspaceRef.current || "").trim();
    activeNativeBuildRouteRef.current = {
      brainName,
      displayName: selectedBrain,
      pipelineId: "",
      processPid: 0,
      requestId: "",
      taskId: "",
      startedAtMs,
      workspaceDir,
    };
    setPipelineClock(startedAtMs);
    setEvent("Native Build Command submitted; waiting for the authoritative backend task event");
  }

  function completeGovernedMotionFromReceipt(receipt: SimulationReceipt, subject: ToolFlowSubject) {
    setTaskRequestPending(false);
    if (!receipt.ok) {
      setBrainMotionState("idle");
      if (subject === "tool") setPipelineTerminalStatus("failed");
      setPhase("failed");
      setProgress((value) => Math.max(8, value));
      return;
    }
    const result = resultRecord(receipt);
    const status = String(result.status || "PASS");
    if (subject === "tool") {
      pipelineStageReceiptsRef.current = new Set(
        Array.from({ length: evidenceToolFlow.length }, (_, index) => index + 1),
      );
      setPipelineTerminalStatus("completed");
    } else if (subject === "refresh") {
      if (!["AWAITING_FUSE", "NO_CHANGE"].includes(status)) {
        setBrainMotionState("idle");
        setPhase("failed");
        setEvent(`Refresh receipt rejected unexpected status ${status}`);
        return;
      }
      const candidateId = String(result.candidate_id || "");
      const candidateReady = status === "AWAITING_FUSE" && Boolean(candidateId);
      setRefreshCandidateId(candidateId || null);
      if (candidateReady) setRefreshReady(true);
      else setRefreshReady(false);
      setRefreshComplete(status === "AWAITING_FUSE" || status === "NO_CHANGE");
      setFuseComplete(false);
      setBrainMotionState("refresh-pulse");
    } else if (subject === "fuse") {
      if (!["FUSED", "PASS_FUSED_AS_NEW_IMMUTABLE_VERSION"].includes(status)) {
        setBrainMotionState("idle");
        setPhase("failed");
        setEvent(`Fuse receipt rejected unexpected status ${status}`);
        return;
      }
      setRefreshReady(false);
      setFuseComplete(false);
      setFuseComplete(status === "FUSED" || status === "PASS_FUSED_AS_NEW_IMMUTABLE_VERSION");
    }
    setMotionReceiptReady(true);
  }

  async function latestVerifiedSnapshotId() {
    const receipt = await execute("brain.versions.list", "Verified immutable brain versions loaded for the Refresh baseline", { brain_name: selectedBrain, verify_hashes: true });
    const rows = backendVersions(receipt);
    setVersions(rows);
    if (rows.length) setSelectedVersion(rows[0].id);
    if (!receipt.ok || !rows.length) throw new Error(receipt.error || "REFRESH_VERIFIED_BASELINE_REQUIRED");
    return rows[0].id;
  }

  async function startGovernedToolchainAction(subject: Exclude<ToolFlowSubject, "tool">) {
    if (building) return;
    if (subject === "fuse" && (!refreshReady || !refreshCandidateId || !FIRST_M0_HIL_PASSED)) {
      const reason = !refreshReady || !refreshCandidateId
        ? "Fuse blocked: Refresh must produce a ready real candidate first"
        : "Fuse blocked: first M0 HIL pass is still required";
      await execute("brain.refresh.status", reason, { brain_name: selectedBrain, candidate_id: refreshCandidateId });
      return;
    }
    startReceiptBoundMotion(subject);
    if (subject === "refresh") {
      setRefreshReady(false);
      setRefreshComplete(false);
      setRefreshCandidateId(null);
      setFuseComplete(false);
      try {
        const expectedSnapshotId = await latestVerifiedSnapshotId();
        const receipt = await execute("brain.refresh.start", "Refresh entered the governed Toolchain lens", {
          brain_name: selectedBrain,
          expected_snapshot_id: expectedSnapshotId,
        });
        completeGovernedMotionFromReceipt(receipt, "refresh");
        // Restore the authoritative terminal task for success and failure;
        // otherwise a failed Refresh leaves the previous completed Build on
        // screen after the backend has already written the newer event.
        await selectBrain(selectedBrain, true, true, false);
        if (!receipt.ok) {
          setEvent(`Refresh failed closed · ${receipt.error || "NATIVE_BACKEND_COMMAND_FAILED"}`);
        }
      } catch (error) {
        setTaskRequestPending(false);
        setBrainMotionState("idle");
        setPhase("failed");
        setEvent(`Refresh failed closed · ${String(error instanceof Error ? error.message : error)}`);
      }
      return;
    }
    const receipt = await execute("brain.refresh.fuse", "Explicit Fuse entered the governed Toolchain lens", {
      brain_name: selectedBrain,
      candidate_id: refreshCandidateId,
      actor: "Evidence OS user",
      reason: "Explicit user Fuse after Refresh tests and HIL pass",
    });
    completeGovernedMotionFromReceipt(receipt, "fuse");
    if (receipt.ok) await selectBrain(selectedBrain, true, false);
  }

  async function launchConfiguredApplication(
    application: "ollama" | "codex",
    fallbackPopup: "ollama" | "codex-handoff",
    command: "ollama.launch" | "codex.launch",
  ) {
    const receipt = await execute(command, `Direct ${application} application launch requested`);
    const status = String(resultRecord(receipt).status || "");
    if (receipt.ok && status === "LAUNCHED") {
      setEvent(`${application} launched through the governed backend · ${receipt.id}`);
      return;
    }
    setPopupReceipt(receipt);
    setPopup(fallbackPopup);
    setEvent(`APPLICATION_NOT_AVAILABLE · ${application} · configure or install before retry`);
  }

  async function launchBrowserProvider(provider: "chatgpt" | "gemini") {
    if (window.__EVIDENCE_OS_NATIVE_COMMAND_TRANSPORT__) {
      const receipt = await execute(
        "shell.external.launch",
        `${provider} opened in the Chrome Default profile with native foreground handoff`,
        { target: provider },
      );
      if (!receipt.ok) {
        setPopupReceipt(receipt);
        setEvent(`BROWSER_FOREGROUND_HANDOFF_FAILED · ${provider} · ${receipt.error || receipt.id}`);
      }
      return;
    }
    setEvent(`NATIVE_PROVIDER_HANDOFF_UNAVAILABLE · ${provider} · native app transport is required`);
  }

  function registeredSourcesForBuild() {
    return (selectedBrainContext?.sources || []).map((source) => {
      const value = String(source.path || source.text || "");
      return {
        source_id: source.source_id,
        lane_key: source.lane_key,
        source_type: source.source_type || source.lane_key,
        display_name: source.display_name || value.split(/[\\/]/).filter(Boolean).at(-1) || value,
        path: source.path || value,
        active: true,
      };
    });
  }

  async function command(key: CommandKey) {
    if (key === "source") openPopup("source-intake", "source.intake.open", "Source Intake opened");
    if (key === "brain-output") {
      const brainRoot = brainOutputRoutes[selectedBrain] || captureBrainOutputRoute(defaultOutputRoot, selectedBrain);
      void execute("folder.open", "Brain packages opened", { ...governedBrainRoute(), path: `${brainRoot}\\packages` });
    }
    if (key === "codex") await launchConfiguredApplication("codex", "codex-handoff", "codex.launch");
    if (key === "chatgpt") await launchBrowserProvider("chatgpt");
    if (key === "ollama") await launchConfiguredApplication("ollama", "ollama", "ollama.launch");
    if (key === "gemini") await launchBrowserProvider("gemini");
    if (key === "build") {
      const route = governedBrainRoute();
      startNativeBuildMotion(route);
      try {
        const receipt = await execute("brain.buildAll", "Build Command started the real coded pipeline and same-pass packages", {
          ...route,
          sources: registeredSourcesForBuild(),
          request_text: "Evidence Lane Build Command requested from the governed native SQLite Builder.",
        });
        completeGovernedMotionFromReceipt(receipt, "tool");
        await selectBrain(selectedBrain, true, false, true, true);
      } finally {
        activeNativeBuildRouteRef.current = null;
      }
    }
  }

  async function chooseCharacterGlb(slot: CharacterGlbSlot) {
    try {
      const errors = characterGlbErrors[slot];
      const pickerReceipt = await execute("character.glb.choose", `Native Windows character ${slot} GLB picker opened`, { slot });
      if (!pickerReceipt.ok) throw new Error(pickerReceipt.error || errors.header);
      const pickerResult = resultRecord(pickerReceipt);
      if (String(pickerResult.status || "") === "CANCELLED") return;
      const asset = backendCharacterGlb(pickerResult);
      if (!asset) throw new Error(errors.header);
      const settingsKey = slot === "face" ? "character_face_glb" : "character_body_glb";
      const receipt = await execute("settings.update", `Character ${slot} GLB stored until changed: ${asset.name}`, {
        category: "interface",
        values: { [settingsKey]: { data_url: asset.dataUrl, name: asset.name, sha256: asset.sha256 } },
      });
      if (!receipt.ok) throw new Error(receipt.error || errors.persistence);
      const values: Partial<StoredSimulationProfile> = slot === "face"
        ? { characterFaceGlbDataUrl: asset.dataUrl, characterFaceGlbName: asset.name, characterFaceGlbSha256: asset.sha256 }
        : { characterBodyGlbDataUrl: asset.dataUrl, characterBodyGlbName: asset.name, characterBodyGlbSha256: asset.sha256 };
      if (slot === "face") {
        setCharacterFaceGlbDataUrl(asset.dataUrl);
        setCharacterFaceGlbName(asset.name);
        setCharacterFaceGlbSha256(asset.sha256);
      } else {
        setCharacterBodyGlbDataUrl(asset.dataUrl);
        setCharacterBodyGlbName(asset.name);
        setCharacterBodyGlbSha256(asset.sha256);
      }
      if (!persistSimulationProfileState(values)) {
        setEvent("Backend persistence succeeded even when the optional browser metadata cache is unavailable");
      }
    } catch (error) {
      setEvent(`Character ${slot} GLB rejected · ${String(error instanceof Error ? error.message : error)}`);
    }
  }

  async function removeCharacterGlb(slot: CharacterGlbSlot) {
    const settingsKey = slot === "face" ? "character_face_glb" : "character_body_glb";
    const receipt = await execute("settings.update", `Stored character ${slot} GLB removed by the user`, {
      category: "interface",
      values: { [settingsKey]: null },
    });
    if (!receipt.ok) return;
    if (slot === "face") {
      const cached = persistSimulationProfileState({ characterFaceGlbDataUrl: "", characterFaceGlbName: "", characterFaceGlbSha256: "" });
      setCharacterFaceGlbDataUrl("");
      setCharacterFaceGlbName("");
      setCharacterFaceGlbSha256("");
      if (!cached) setEvent("Face GLB was removed from the backend; optional browser metadata cache was unavailable");
    } else {
      const cached = persistSimulationProfileState({ characterBodyGlbDataUrl: "", characterBodyGlbName: "", characterBodyGlbSha256: "" });
      setCharacterBodyGlbDataUrl("");
      setCharacterBodyGlbName("");
      setCharacterBodyGlbSha256("");
      if (!cached) setEvent("Body GLB was removed from the backend; optional browser metadata cache was unavailable");
    }
  }

  async function chooseProfileImage() {
    const pickerReceipt = await execute("profile.image.choose", "Native Windows profile-image picker opened");
    if (!pickerReceipt.ok) return;
    const result = resultRecord(pickerReceipt);
    if (String(result.status || "") === "CANCELLED") return;
    const dataUrl = String(result.data_url || "");
    const name = String(result.name || "");
    const sha256 = String(result.sha256 || "");
    const size = Number(result.size_bytes || 0);
    if (!dataUrl.startsWith("data:image/") || !name || !/^[a-f0-9]{64}$/i.test(sha256) || size <= 0 || size > 3 * 1024 * 1024) {
      setEvent("Profile image rejected · NATIVE_PROFILE_IMAGE_RECEIPT_INVALID");
      return;
    }
    if (!persistSimulationProfileState({ profileImageDataUrl: dataUrl, profileImageName: name })) {
      setEvent("Profile image was not applied because persistent simulation storage is unavailable");
      return;
    }
    setProfileImageUrl(dataUrl);
    setProfileImageName(name);
    await execute("profile.update", `Profile image stored until changed: ${name}`, {
      values: { avatar_ref: `native-profile:${sha256}`, metadata: { profile_image_name: name, profile_image_sha256: sha256 } },
    });
  }

  async function chooseOllamaExecutable() {
    const pickerReceipt = await execute("ollama.executable.choose", "Native Windows Ollama executable picker opened");
    if (!pickerReceipt.ok) return;
    const result = resultRecord(pickerReceipt);
    if (String(result.status || "") === "CANCELLED") return;
    const path = String(result.executable_path || "");
    const displayName = String(result.display_name || "");
    if (!path || !displayName || !/\.(exe|lnk)$/i.test(displayName)) {
      setEvent("Ollama executable rejected · NATIVE_OLLAMA_EXECUTABLE_RECEIPT_INVALID");
      return;
    }
    const settingsReceipt = await execute("ollama.settings.update", `Ollama application route stored: ${displayName}`, {
      values: { ollama_executable_override: path },
    });
    setPopupReceipt(settingsReceipt);
    if (settingsReceipt.ok) setOllamaExecutablePath(path);
  }

  async function openVersionControl() {
    setPopup("version-control");
    setPopupReceipt(null);
    setRollbackResult("");
    setVersionDropArmed(false);
    setVersionDropConfirmation("");
    setVersionDropResult("");
    const receipt = await execute("brain.versions.list", "Real immutable versions loaded", {
      ...governedBrainRoute(),
      verify_hashes: true,
    });
    setPopupReceipt(receipt);
    const rows = backendVersions(receipt);
    setVersions(rows);
    // Version 0 is the immutable current head and cannot be dropped. Select
    // the newest historical rollback by default so both governed actions are
    // immediately operable whenever history exists.
    setSelectedVersion(rows[1]?.id || "");
  }

  async function dropSelectedHistoricalVersion() {
    const currentVersionId = versions[0]?.id || "";
    if (!selectedVersion || selectedVersion === currentVersionId || versionDropConfirmation !== selectedVersion) return;
    const receipt = await execute("brain.version.drop", `${selectedVersion} historical rollback record moved to the Windows Recycle Bin`, {
      ...governedBrainRoute(),
      version_id: selectedVersion,
      confirm_version_id: versionDropConfirmation,
      actor_name: "Evidence OS user",
      reason: "Explicit historical rollback drop from Brain Version Control",
    });
    setPopupReceipt(receipt);
    if (!receipt.ok) {
      setVersionDropResult(receipt.error || "The historical version was not dropped.");
      return;
    }
    const refreshed = await execute("brain.versions.list", "Verified immutable versions reloaded after historical drop", {
      ...governedBrainRoute(),
      verify_hashes: true,
    });
    const rows = backendVersions(refreshed);
    setVersions(rows);
    setSelectedVersion(rows[1]?.id || "");
    setVersionDropArmed(false);
    setVersionDropConfirmation("");
    setVersionDropResult(`${selectedVersion} moved to the Windows Recycle Bin. The current brain did not change.`);
  }

  async function openFlashPrompt() {
    setPopup("flash");
    setPopupReceipt(null);
    const receipt = await execute("flash.read", "Complete flash prompt read from the real backend", { brain_name: selectedBrain });
    setPopupReceipt(receipt);
    const prompt = String(resultRecord(receipt).prompt || "");
    setFlashPrompt(receipt.ok ? prompt : "");
  }

  async function loadPlanGoalState() {
    const route = governedBrainRoute();
    const promptReceipt = await execute(
      "brain.planGoalPrompt.read",
      "Canonical Prompt 2 materialized from the active Plan delta chain",
      route,
    );
    setPopupReceipt(promptReceipt);
    setPlanGoalPrompt(promptReceipt.ok ? String(resultRecord(promptReceipt).text || "") : "");
    const stateReceipt = await execute(
      "brain.planDelta.state",
      "Classified Plan and steer delta ledger read from the selected brain",
      route,
    );
    setPopupReceipt(stateReceipt);
    const state = stateReceipt.ok ? resultRecord(stateReceipt) : null;
    setPlanDeltaState(state);
    const rows = Array.isArray(state?.deltas)
      ? state.deltas.filter((row): row is Record<string, unknown> => Boolean(row && typeof row === "object" && !Array.isArray(row)))
      : [];
    const pointer = state?.canonical_pointer && typeof state.canonical_pointer === "object" && !Array.isArray(state.canonical_pointer)
      ? state.canonical_pointer as Record<string, unknown>
      : {};
    const canonicalId = String(pointer.current_delta_id || "");
    setSelectedPlanDelta((current) => (
      rows.some((row) => String(row.delta_id || "") === current)
        ? current
        : canonicalId || String(rows.at(-1)?.delta_id || "")
    ));
  }

  async function openPlanGoal() {
    setPopup("plan-goal");
    setPopupReceipt(null);
    setPlanDeltaActionResult("");
    await loadPlanGoalState();
  }

  async function appendPlanSteer() {
    const content = planSteerDraft.trim();
    if (!content) return;
    const receipt = await execute(
      "brain.planDelta.append",
      "Additive steer classified and appended to the selected brain Plan ledger",
      {
        ...governedBrainRoute(),
        content,
        actor: "Evidence OS user",
        model_name: "evidence-os-plan-ui",
        model_family: "HUMAN",
        turn_id: `plan-ui-${Date.now()}`,
        next_action: "CONTINUE_SAME_LINEAR_GOAL_UNTIL_HIL",
      },
    );
    setPopupReceipt(receipt);
    if (!receipt.ok) {
      setPlanDeltaActionResult(receipt.error || "The steer delta was not appended.");
      return;
    }
    setPlanSteerDraft("");
    setPlanDeltaActionResult(`Delta ${String(resultRecord(receipt).delta_sequence || "")} appended as PENDING and classified automatically.`);
    await loadPlanGoalState();
  }

  async function setPlanDisposition(disposition: "APPROVED" | "SUPERSEDED" | "CHANGED" | "FAILED" | "DROPPED") {
    if (!selectedPlanDelta) return;
    const receipt = await execute(
      "brain.planDelta.disposition",
      `${selectedPlanDelta} classified ${disposition}`,
      {
        ...governedBrainRoute(),
        delta_id: selectedPlanDelta,
        disposition,
        actor: "Evidence OS user",
        reason: "Explicit Plan delta classification from the governed Plan Goal popup",
        turn_id: `plan-disposition-${Date.now()}`,
      },
    );
    setPopupReceipt(receipt);
    setPlanDeltaActionResult(receipt.ok
      ? `${selectedPlanDelta} is now ${disposition}; its history remains preserved.`
      : receipt.error || "The delta disposition was not changed.");
    if (receipt.ok) await loadPlanGoalState();
  }

  function taskAction(action: TaskAction) {
    if (action === "rollback") void openVersionControl();
    if (action === "pause") {
      setPaused((current) => !current);
      execute("task.pause", paused ? "Pipeline simulation resumed" : "Pipeline simulation paused");
    }
    if (action === "view") openPopup("task-details", "task.view", "System Task details opened");
    if (action === "flash") void openFlashPrompt();
    if (action === "output") {
      setPopup("brain-output");
      const brainRoot = brainOutputRoutes[selectedBrain] || captureBrainOutputRoute(defaultOutputRoot, selectedBrain);
      void execute("folder.open", "Task packages opened", { ...governedBrainRoute(), path: `${brainRoot}\\packages` });
    }
  }

  function commitSelectedBrainContext(context: SelectedBrainSimulationContext, displayName: string, restorePipeline = true, commitAuthority = true) {
    const brainName = context.brain_identity.brain_name;
    selectedBrainContextCacheRef.current[displayName] = context;
    selectedBrainContextCacheRef.current[brainName] = context;
    setSelectedBrain(displayName);
    setSelectedBrainContext(context);
    if (commitAuthority) {
      setCommittedBrain(displayName);
      setSelectionAuthorityMode("COMMITTED");
    }
    setLaneSchemas(Object.fromEntries(
      Object.entries(context.lane_schemas).map(([laneId, row]) => [laneId, row.schema.map(String)]),
    ));
    setLaneSchemaVersions(Object.fromEntries(
      Object.entries(context.lane_schemas).map(([laneId, row]) => [laneId, Number(row.version || 0)]),
    ));
    const nextVersions = contextVersionViews(context);
    setVersions(nextVersions);
    setSelectedVersion(nextVersions[0]?.id || "");
    const outputDir = String(context.output_route.output_dir || context.brain_identity.output_dir || "");
    if (outputDir) setBrainOutputRoutes((current) => ({ ...current, [displayName]: outputDir }));
    const candidateId = String(context.refresh_candidate.candidate_id || "");
    const candidateStatus = String(context.refresh_candidate.status || "");
    const candidateReady = Boolean(candidateId) && candidateStatus === "AWAITING_FUSE";
    setRefreshCandidateId(candidateReady ? candidateId : null);
    setRefreshReady(candidateReady);
    setRefreshComplete(candidateReady);
    setFuseComplete(candidateStatus === "FUSED");
    if (restorePipeline) {
      const pipeline = context.pipeline_state?.task_event;
      const restoredTaskIdentity = universalTaskSurfaceIdentity(pipeline);
      setShowRestoredPipeline(true);
      if (pipeline && restoredTaskIdentity) {
        const status = String(pipeline.status || "queued").toLowerCase();
        const restoredProgress = Math.max(0, Math.min(100, Number(pipeline.global_percent ?? pipeline.progress) || 0));
        const restoredToolIndex = Math.max(0, Math.min(evidenceToolFlow.length - 1, Number(pipeline.stage_order || 1) - 1));
        const restoredCompleted = status === "completed" && ["brain.build", "brain.buildAll"].includes(String(pipeline.command || ""))
          ? evidenceToolFlow.length
          : Math.max(0, Number(pipeline.completed_count) || restoredToolIndex);
        pipelineMotionKeyRef.current = restoredTaskIdentity.taskId;
        pipelineStageReceiptsRef.current = new Set(
          Array.from({ length: restoredCompleted }, (_, index) => index + 1),
        );
        toolIndexRef.current = restoredToolIndex;
        setPipelineTerminalStatus(["completed", "hil_waiting", "skipped"].includes(status) ? "completed" : status === "failed" || status === "cancelled" ? status : "");
        setMotionSubject(pipeline.command === "brain.refresh.start" ? "refresh" : pipeline.command === "brain.refresh.fuse" ? "fuse" : "tool");
        setPaused(false);
        setProgress(restoredProgress);
        setToolIndex(restoredToolIndex);
        setMotionReceiptReady(["completed", "hil_waiting", "skipped"].includes(status));
        setBrainMotionState(status === "completed" ? "success-flash" : "idle");
        if (["completed", "hil_waiting", "skipped"].includes(status)) setPhase("complete");
        else if (status === "failed" || status === "cancelled") setPhase("failed");
        else if (status === "running") setPhase("scanning");
        else setPhase("idle");
      } else {
        pipelineMotionKeyRef.current = "";
        pipelineStageReceiptsRef.current = new Set();
        toolIndexRef.current = 0;
        setPipelineTerminalStatus("");
        setMotionSubject("tool");
        setMotionReceiptReady(false);
        setBrainMotionState("idle");
        setPaused(false);
        setToolIndex(0);
        setProgress(0);
        setPhase("idle");
      }
    }
  }

  async function selectBrain(
    name: string,
    preservePopup = false,
    restorePipeline = true,
    persistSelection = true,
    retainLivePipeline = false,
  ) {
    const generation = ++brainSelectionGenerationRef.current;
    const selectionRequestId = `ui-brain-selection-${Date.now()}-${generation}`;
    if (!retainLivePipeline) {
      setLivePipeline(null);
      setLiveTaskHistory([]);
      activeNativeBuildRouteRef.current = null;
    }
    const selectionStartedAt = performance.now();
    const cachedContext = selectedBrainContextCacheRef.current[name]
      || selectedBrainContextCacheRef.current[brainCatalogByDisplayRef.current[name]?.brainName || ""];
    setSelectionAuthorityMode("PENDING");
    if (cachedContext) commitSelectedBrainContext(cachedContext, name, restorePipeline, false);
    let visibleSwitchSettled = false;
    let visibleSwitchFallback = 0;
    const visibleSwitchPromise = new Promise<number>((resolve) => {
      const finishVisibleSwitch = () => {
        if (visibleSwitchSettled) return;
        visibleSwitchSettled = true;
        if (visibleSwitchFallback) window.clearTimeout(visibleSwitchFallback);
        resolve(performance.now() - selectionStartedAt);
      };
      window.requestAnimationFrame(finishVisibleSwitch);
      visibleSwitchFallback = window.setTimeout(finishVisibleSwitch, 64);
    });
    // An uncached selection is atomic: the prior complete brain remains visible
    // until brain.select returns the next complete context and stored telemetry
    // bundle. Never clear the current topology, pipeline, or source state while
    // a backend context is pending.
    if (!preservePopup) closePopup();
    const contextReceiptPromise = execute(
      "brain.select",
      `${name} selected with one complete backend context`,
      {
        brain_name: name,
        selection_restore_only: !persistSelection,
        selection_request_id: selectionRequestId,
      },
    );
    const receipt = await contextReceiptPromise;
    if (generation !== brainSelectionGenerationRef.current) return;
    const result = resultRecord(receipt);
    const identity = result.brain_identity;
    const selectionAuthority = result.selection_authority;
    const selectionCommit = result.selection_commit;
    const expectedBrainName = brainCatalogByDisplayRef.current[name]?.brainName || name;
    const expectedCommitStatus = persistSelection ? "COMMITTED" : "RESTORED";
    if (
      !receipt.ok
      || result.context_schema !== "T023_SELECTED_BRAIN_CONTEXT_V1"
      || !identity
      || typeof identity !== "object"
      || Array.isArray(identity)
      || String((identity as Record<string, unknown>).brain_name || "") !== expectedBrainName
      || !selectionAuthority
      || typeof selectionAuthority !== "object"
      || Array.isArray(selectionAuthority)
      || String((selectionAuthority as Record<string, unknown>).schema || "") !== t023UiSystemSchemas.selectedBrainAuthority
      || String((selectionAuthority as Record<string, unknown>).brain_id || "") !== String((identity as Record<string, unknown>).brain_id || "")
      || String((selectionAuthority as Record<string, unknown>).brain_name || "") !== expectedBrainName
      || !selectionCommit
      || typeof selectionCommit !== "object"
      || Array.isArray(selectionCommit)
      || String((selectionCommit as Record<string, unknown>).selection_request_id || "") !== selectionRequestId
      || String((selectionCommit as Record<string, unknown>).status || "") !== expectedCommitStatus
      || String((selectionCommit as Record<string, unknown>).context_sha256 || "") !== String(result.context_sha256 || "")
      || !Array.isArray(result.sources)
      || !result.lane_schemas
    ) {
      setEvent(`${name} selection failed closed: complete selected-brain context was not returned`);
      setSelectionAuthorityMode(selectedBrainContext ? "COMMITTED" : "BROWSE_ONLY_BACKEND_UNAVAILABLE");
      return;
    }
    const context = result as unknown as SelectedBrainSimulationContext;
    commitSelectedBrainContext(context, name, restorePipeline);
    const completeContextMs = performance.now() - selectionStartedAt;
    const visibleSwitchMs = await visibleSwitchPromise;
    const backendContextMs = Number(context.selection_context_metrics?.backend_context_ms || 0);
    void execute(
      "brain.selection.timing.record",
      `${name} visible in ${visibleSwitchMs.toFixed(1)} ms; complete context in ${completeContextMs.toFixed(1)} ms`,
      {
        workspace_dir: activeWorkspaceRef.current,
        brain_display_name: name,
        routed_brain_name: expectedBrainName,
        visible_switch_ms: Number(visibleSwitchMs.toFixed(3)),
        complete_context_ms: Number(completeContextMs.toFixed(3)),
        backend_context_ms: Number(backendContextMs.toFixed(3)),
        context_sha256: context.context_sha256,
        context_reuse_contract: context.context_reuse_contract,
      },
    );
  }

  function toggleBrainPin(name: string) {
    const pinned = pinnedBrains.includes(name);
    setPinnedBrains((current) => pinned ? current.filter((item) => item !== name) : [...current, name]);
    execute(pinned ? "brain.unpin" : "brain.pin", `${name} ${pinned ? "unpinned" : "pinned"}`, { brain_name: name });
    setBrainMenu(null);
  }

  async function commitBrainRename() {
    if (!editingBrain) return;
    const priorName = editingBrain;
    const next = renameValue.trim();
    setEditingBrain(null);
    setRenameValue("");
    if (next && next !== priorName && !brains.includes(next)) {
      const priorEntry = brainCatalogByDisplayRef.current[priorName];
      const receipt = await execute("brain.rename", `${priorName} renamed inline to ${next}`, { brain_name: priorName, new_brain_name: next });
      if (!receipt.ok) return;
      const catalogReceipt = await execute("brains.catalog", `${next} rename propagated to the governed brain catalog`);
      const catalog = catalogReceipt.ok ? commitBrainCatalog(resultRecord(catalogReceipt)) : [];
      const renamed = catalog.find((entry) => entry.brainName === next && entry.workspaceDir.toLowerCase() === (priorEntry?.workspaceDir || "").toLowerCase());
      const nextDisplayName = renamed?.displayName || next;
      setChats((current) => current.map((chat) => chat.brain === priorName ? { ...chat, brain: nextDisplayName } : chat));
      setBrainColors((current) => {
        const updated = { ...current, [nextDisplayName]: current[priorName] || brainColor(priorName) };
        delete updated[priorName];
        return updated;
      });
      if (selectedBrain === priorName) await selectBrain(nextDisplayName, true);
    }
  }

  function updateLaneField(key: string, value: string) {
    if (!activeLane) return;
    setLaneDrafts((current) => ({ ...current, [activeLane.key]: { ...(current[activeLane.key] || {}), [key]: value } }));
  }

  function sourceSelectionValue(lane: EvidenceLane) {
    if (lane.kind === "github") return (laneDrafts[lane.key]?.repo_url || "").trim();
    return (laneDrafts[lane.key]?.source_selection || "").trim();
  }

  function sourceSelectionPaths(lane: EvidenceLane) {
    if (lane.kind === "github") return [];
    try {
      const paths = JSON.parse(laneDrafts[lane.key]?.source_paths || "[]");
      return Array.isArray(paths) ? paths.map(String).filter(Boolean) : [];
    } catch {
      return [];
    }
  }

  async function requestNativeSourcePicker(kind?: "file" | "folder") {
    if (!activeLane || sourcePickerInFlightRef.current) return;
    const lane = activeLane;
    const policy = evidenceLanePickerPolicy[lane.key] || evidenceLanePickerPolicy.custom;
    const mode = kind || (policy.kind === "folder" ? "folder" : "file");
    const registerSingleFolderSelection = async (paths: string[], displayNames: string[]) => {
      if (paths.length !== 1 || displayNames.length !== 1) throw new Error("LOCAL_CODE_SINGLE_FOLDER_REQUIRED");
      const selectedPath = paths[0];
      const displayName = displayNames[0] || selectedPath.split(/[\\/]/).filter(Boolean).at(-1) || lane.label;
      const contract = "SOURCE_PICKER_SINGLE_DIALOG_ATOMIC_REGISTER";
      const validationReceipt = await execute("source.lane.validate", `${lane.label} single-folder selection validated and registration authorized`, {
        brain_name: selectedBrain,
        lane_key: lane.key,
        path: selectedPath,
        picker_contract: contract,
      });
      if (!validationReceipt.ok) return;
      setValidatedLanes((current) => current.includes(lane.key) ? current : [...current, lane.key]);
      const alreadyLoaded = (contextSourcesByLane[lane.key] || []).some((value) => value.toLowerCase() === selectedPath.toLowerCase());
      if (!alreadyLoaded) {
        const registrationReceipt = await execute("sources.add", `${lane.label} source atomically registered from its single native folder selection: ${displayName}`, {
          brain_name: selectedBrain,
          lane_key: lane.key,
          path: selectedPath,
          display_name: displayName,
          source_type: lane.label,
          schema_contract: lane.schema.join("\n"),
          schema_version: laneSchemaVersions[lane.key] || 0,
          picker_contract: contract,
        });
        if (!registrationReceipt.ok) return;
      }
      await selectBrain(selectedBrain, true);
    };
    sourcePickerInFlightRef.current = true;
    try {
      const receipt = await execute("source.path.choose", `${lane.label} native Windows File Explorer picker opened`, {
        brain_name: selectedBrain,
        lane_key: lane.key,
        picker_kind: mode,
      });
      const result = resultRecord(receipt);
      if (!receipt.ok || String(result.status || "") === "CANCELLED") return;
      const paths = Array.isArray(result.paths) ? result.paths.map(String).filter(Boolean) : [];
      const displayNames = Array.isArray(result.display_names) ? result.display_names.map(String).filter(Boolean) : [];
      if (!paths.length || paths.length !== displayNames.length) throw new Error("PICKER_SELECTION_RECEIPT_INVALID");
      setLaneDrafts((current) => ({
        ...current,
        [lane.key]: {
          ...(current[lane.key] || {}),
          source_selection: displayNames.join(", "),
          source_paths: JSON.stringify(paths),
        },
      }));
      setValidatedLanes((current) => current.filter((key) => key !== lane.key));
      if (lane.key === "local_code") await registerSingleFolderSelection(paths, displayNames);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setEvent(`${lane.label} native source picker failed closed · ${String(error instanceof Error ? error.message : error)}`);
    } finally {
      sourcePickerInFlightRef.current = false;
    }
  }

  async function inspectLaneSchema() {
    if (!activeLane) return;
    setSchemaInspectorLaneKey(activeLane.key);
    setSchemaInspectorDraft(activeLane.schema.join("\n"));
    setSchemaInspectorError("");
    setSchemaInspectorBusy(true);
    const receipt = await execute("source.schema.get", `${activeLane.label} governed schema loaded`, { brain_name: selectedBrain, lane_key: activeLane.key });
    setSchemaInspectorBusy(false);
    const result = resultRecord(receipt);
    const contract = String(result.schema_contract || "").trim();
    const schema = Array.isArray(result.schema) ? result.schema.map(String) : [];
    if (!receipt.ok) {
      setSchemaInspectorError(receipt.error || "The governed schema could not be loaded.");
      return;
    }
    if (contract) setSchemaInspectorDraft(contract);
    if (schema.length) setLaneSchemas((current) => ({ ...current, [activeLane.key]: schema }));
    setLaneSchemaVersions((current) => ({ ...current, [activeLane.key]: Number(result.version || 0) }));
  }

  async function saveLaneSchema() {
    if (!activeLane || !schemaInspectorDraft.trim() || schemaInspectorBusy) return;
    setSchemaInspectorBusy(true);
    setSchemaInspectorError("");
    const receipt = await execute("source.schema.update", `${activeLane.label} governed schema saved`, {
      brain_name: selectedBrain,
      lane_key: activeLane.key,
      schema_contract: schemaInspectorDraft,
      actor: "Evidence OS simulation user",
    });
    setSchemaInspectorBusy(false);
    const result = resultRecord(receipt);
    if (!receipt.ok) {
      setSchemaInspectorError(receipt.error || "The schema was rejected.");
      return;
    }
    const schema = Array.isArray(result.schema) ? result.schema.map(String) : schemaInspectorDraft.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    setLaneSchemas((current) => ({ ...current, [activeLane.key]: schema }));
    setLaneSchemaVersions((current) => ({ ...current, [activeLane.key]: Number(result.version || 0) }));
    setSchemaInspectorDraft(schema.join("\n"));
    await selectBrain(selectedBrain, true);
  }

  async function resetLaneSchema() {
    if (!activeLane || schemaInspectorBusy) return;
    setSchemaInspectorBusy(true);
    setSchemaInspectorError("");
    const receipt = await execute("source.schema.reset", `${activeLane.label} built-in schema restored`, { brain_name: selectedBrain, lane_key: activeLane.key, actor: "Evidence OS simulation user" });
    setSchemaInspectorBusy(false);
    const result = resultRecord(receipt);
    if (!receipt.ok) {
      setSchemaInspectorError(receipt.error || "The built-in schema could not be restored.");
      return;
    }
    const schema = Array.isArray(result.schema) ? result.schema.map(String) : activeLane.schema;
    setLaneSchemas((current) => ({ ...current, [activeLane.key]: schema }));
    setLaneSchemaVersions((current) => ({ ...current, [activeLane.key]: Number(result.version || 0) }));
    setSchemaInspectorDraft(schema.join("\n"));
    await selectBrain(selectedBrain, true);
  }

  function validateLane() {
    if (!activeLane) return false;
    const value = sourceSelectionValue(activeLane);
    if (!value) {
      execute("source.lane.validate", `${activeLane.label} validation blocked: native source selection required`);
      setValidatedLanes((current) => current.filter((key) => key !== activeLane.key));
      return false;
    }
    execute("source.lane.validate", `${activeLane.label} field and universal-schema contract validated`);
    setValidatedLanes((current) => current.includes(activeLane.key) ? current : [...current, activeLane.key]);
    return true;
  }

  async function addSource() {
    if (!activeLane || !validateLane()) return;
    const value = sourceSelectionValue(activeLane);
    if (activeLane.kind === "github") {
      const payload = {
          brain_name: selectedBrain,
          repo_url: activeLaneDraft.repo_url || value,
          branch: activeLaneDraft.branch || "",
          token: activeLaneDraft.private_token || "",
          target_name: activeLaneDraft.folder_name || "",
          full_history: true,
          pull_existing: true,
        };
      const receipt = await execute("sources.cloneGithub", `${activeLane.label} repository registered through the governed Git connector`, payload);
      if (receipt.ok) await selectBrain(selectedBrain, true);
      return;
    }
    const selectedPaths = sourceSelectionPaths(activeLane);
    if (!selectedPaths.length) {
      await execute("source.lane.validate", `${activeLane.label} registration blocked: native picker receipt required`);
      return;
    }
    const alreadyLoaded = new Set(contextSourcesByLane[activeLane.key] || []);
    let added = 0;
    for (const selectedPath of selectedPaths) {
      if (alreadyLoaded.has(selectedPath)) continue;
      const displayName = selectedPath.split(/[\\/]/).filter(Boolean).at(-1) || activeLane.label;
      const payload = {
          brain_name: selectedBrain,
          lane_key: activeLane.key.startsWith("custom_") ? "custom" : activeLane.key,
          path: selectedPath,
          display_name: displayName,
          source_type: activeLane.label,
          schema_contract: activeLane.schema.join("\n"),
          schema_version: laneSchemaVersions[activeLane.key] || 0,
          custom_lane_name: activeLane.key.startsWith("custom_") ? activeLane.label : "",
        };
      const receipt = await execute("sources.add", `${activeLane.label} source registered from native selection: ${displayName}`, payload);
      if (receipt.ok) added += 1;
    }
    if (added) await selectBrain(selectedBrain, true);
    else await execute("source.lane.validate", `${activeLane.label} identical sources reused; zero duplicate source rows`);
  }

  async function removeSource(laneKey: string, value: string) {
    let row = selectedBrainContext?.sources.find((item) => String(item.path || item.text || "") === value) as Record<string, unknown> | undefined;
    let listReceipt: SimulationReceipt | null = null;
    if (!row) {
      listReceipt = await execute("sources.list", `Resolving real source identity before removal: ${value}`, { brain_name: selectedBrain });
      const sources = resultRecord(listReceipt).sources;
      row = Array.isArray(sources) ? sources.find((item) => item && typeof item === "object" && String((item as Record<string, unknown>).path || (item as Record<string, unknown>).text || "") === value) as Record<string, unknown> | undefined : undefined;
    }
    const sourceId = String(row?.source_id || "");
    if ((listReceipt && !listReceipt.ok) || !sourceId) return;
    const receipt = await execute("sources.remove", `Source removed: ${value}`, { brain_name: selectedBrain, source_id: sourceId });
    if (receipt.ok) await selectBrain(selectedBrain, true);
  }

  async function removeLaneSources(laneKey: string, laneLabel: string) {
    const receipt = await execute("sources.removeLane", `${laneLabel} lane cleared after confirmation contract`, {
      brain_name: selectedBrain,
      lane_key: laneKey,
    });
    if (receipt.ok) await selectBrain(selectedBrain, true);
  }

  async function chooseOutputRoot() {
    setOutputRootResult("Windows Explorer is open. Choose the output root for brains created from now on.");
    const receipt = await execute("workspace.outputRoot.choose", "Native Windows output-root folder picker opened");
    const result = resultRecord(receipt);
    if (!receipt.ok) {
      setOutputRootResult(receipt.error || "The selected folder could not be initialized.");
      return;
    }
    if (String(result.status || "") === "CANCELLED") {
      setOutputRootResult("Folder selection cancelled. The current future-brain output root remains active.");
      return;
    }
    const nextRoot = String(result.workspace_dir || "").trim();
    if (!nextRoot) {
      setOutputRootResult("The backend did not return an active workspace. Nothing changed.");
      return;
    }
    activeWorkspaceRef.current = nextRoot;
    setDefaultOutputRoot(nextRoot);
    setWorkspaceRoots(backendWorkspaceRoots(result));
    const catalog = commitBrainCatalog(result);
    const names = catalog.map((entry) => entry.displayName);
    const nextSelectedBrain = names.includes(selectedBrain) ? selectedBrain : (names[0] || "");
    setSelectedBrain(nextSelectedBrain);
    setSelectedBrainContext(null);
    if (nextSelectedBrain) void selectBrain(nextSelectedBrain, true);
    setOutputRootResult(`${nextRoot} is now the only catalog root. ${names.length} brain project${names.length === 1 ? " is" : "s are"} visible there; older root folders remain untouched on disk.`);
  }

  function createCustomLane() {
    const template = evidenceSourceLanes.find((lane) => lane.key === "custom");
    if (!template) return;
    const sequence = customLanes.length + 2;
    const lane: EvidenceLane = {
      ...template,
      key: `custom_${sequence}`,
      label: `Custom ${sequence}`,
      fields: template.fields.map((field) => ({ ...field })),
      schema: [...template.schema],
      laws: [...template.laws],
    };
    setCustomLanes((current) => [...current, lane]);
    execute("source.lane.customCreate", `${lane.label} schema-first intake pill appended`);
  }

  const searchResults = [
    ...brains.map((name) => ({ id: `brain:${name}`, title: name, meta: "brain", kind: "brain" as const, brain: name })),
    ...chats.map((chat) => ({ id: chat.id, title: chat.title, meta: `chat · ${chat.brain}`, kind: "chat" as const, brain: chat.brain })),
  ]
    .filter((item) => (item.kind !== "chat" || brains.includes(item.brain))
      && (!searchQuery.trim() || `${item.title} ${item.meta}`.toLowerCase().includes(searchQuery.trim().toLowerCase())));
  const authoritativeTaskEvent = livePipeline || selectedBrainContext?.pipeline_state?.task_event || null;
  const authoritativeTaskIdentity = universalTaskSurfaceIdentity(authoritativeTaskEvent);
  const authoritativeTaskHistory = useMemo(() => {
    const taskId = authoritativeTaskIdentity?.taskId || "";
    if (!taskId) return [];
    const candidates = [
      ...(selectedBrainContext?.pipeline_state?.task_history || []),
      ...liveTaskHistory,
      ...(authoritativeTaskEvent ? [authoritativeTaskEvent] : []),
    ];
    const deduplicated = new Map<string, BackendPipelineEvent>();
    for (const taskEvent of candidates) {
      const identity = universalTaskSurfaceIdentity(taskEvent);
      if (!identity || identity.taskId !== taskId) continue;
      const key = [
        identity.taskId,
        taskEvent.update_timestamp || taskEvent.updated_at || "",
        taskEvent.stage_id || "",
        taskEvent.tool_id || "",
        taskEvent.status || "",
        taskEvent.global_percent ?? taskEvent.progress ?? "",
      ].join("|");
      deduplicated.set(key, taskEvent);
    }
    return Array.from(deduplicated.values());
  }, [
    authoritativeTaskEvent,
    authoritativeTaskIdentity?.taskId,
    liveTaskHistory,
    selectedBrainContext?.pipeline_state?.task_history,
  ]);
  const authoritativeToolEvents = authoritativeTaskHistory;
  const motionTaskModel = useMemo<{
    progress: number;
    status: "waiting" | "running" | "completed" | "failed";
    task: string;
    detail: string;
    running: number;
    queued: number;
    completed: number;
    elapsed: string;
    stage: string;
    queue: TaskQueueItem[];
  }>(() => {
    if (!authoritativeTaskEvent || !authoritativeTaskIdentity) {
      const selectionPending = selectionAuthorityMode === "PENDING";
      return {
        progress: 0,
        status: "waiting",
        task: selectionPending ? `Loading ${selectedBrain} saved state` : taskRequestPending ? "Waiting for authoritative backend task event" : "Waiting for Build, Refresh, or Fuse",
        detail: selectionPending ? "The prior brain task surface is cleared while one complete saved backend context is restored." : "No task_id or run_id is active for the selected brain.",
        running: 0,
        queued: 0,
        completed: 0,
        elapsed: "--",
        stage: selectionPending ? "loading saved state" : "idle",
        queue: [],
      };
    }

    const event = authoritativeTaskEvent;
    const eventStatus = authoritativeTaskIdentity.status;
    const taskStatus: "waiting" | "running" | "completed" | "failed" = eventStatus === "failed" || eventStatus === "cancelled"
      ? "failed"
      : eventStatus === "completed" || eventStatus === "skipped"
        ? "completed"
        : eventStatus === "running"
          ? "running"
          : "waiting";
    const terminal = taskStatus === "completed" || taskStatus === "failed";
    const progressPercent = Math.max(0, Math.min(100, Number(event.global_percent ?? event.progress) || 0));
    const stageCount = Math.max(1, Number(event.stage_count) || 1);
    const currentOrder = Math.max(1, Math.min(stageCount, Number(event.stage_order) || 1));
    const commandName = String(event.active_command || event.command || motionSubject || "task").trim();
    const isTenStageBuild = stageCount === backendPipelineStages.length
      && (commandName.toLowerCase().includes("build") || backendPipelineStages.some((row) => row.stage_id === event.stage_id));
    const authoritativeStages = isTenStageBuild
      ? backendPipelineStages
      : [{
          stage_id: authoritativeTaskIdentity.stageId || `${commandName || "task"}-stage`,
          stage_name: authoritativeTaskIdentity.stageName,
          stage_order: currentOrder,
        }];
    const eventQueue: TaskQueueItem[] = authoritativeStages.map((row, index) => {
      const order = isTenStageBuild ? Number(row.stage_order || index + 1) : currentOrder;
      const state: TaskQueueItem["state"] = taskStatus === "failed" && order === currentOrder
        ? "failed"
        : taskStatus === "completed" || order < currentOrder
          ? "complete"
          : taskStatus === "running" && order === currentOrder
            ? "running"
            : "queued";
      return {
        id: String(row.stage_id || `task-stage-${index + 1}`),
        label: String(row.stage_name || authoritativeTaskIdentity.stageName),
        state,
      };
    });
    const backendElapsed = Math.max(0, Number(event.elapsed_seconds) || 0);
    const startedAtMs = Date.parse(String(event.started_at || event.start_timestamp || ""));
    const liveElapsed = taskStatus === "running" && Number.isFinite(startedAtMs)
      ? Math.max(backendElapsed, Math.max(0, (pipelineClock - startedAtMs) / 1000))
      : backendElapsed;
    const pidLabel = authoritativeTaskIdentity.processPid > 0
      ? `PID ${authoritativeTaskIdentity.processPid}`
      : "PID unavailable";
    const derivedRunning = eventQueue.filter((row) => row.state === "running").length;
    const derivedQueued = eventQueue.filter((row) => row.state === "queued").length;
    const derivedCompleted = eventQueue.filter((row) => row.state === "complete").length;
    const numericOr = (value: unknown, fallback: number) => Number.isFinite(Number(value)) ? Number(value) : fallback;
    const failureCode = String(event.failure_code || event.error_code || "").trim();
    return {
      progress: progressPercent,
      status: taskStatus,
      task: `${commandName || "Authoritative backend task"} · ${pidLabel}`,
      detail: `Task ${authoritativeTaskIdentity.taskId} · Run ${authoritativeTaskIdentity.runId}`,
      running: numericOr(event.running_count, derivedRunning),
      queued: numericOr(event.queued_count, derivedQueued),
      completed: numericOr(event.completed_count, derivedCompleted),
      elapsed: liveElapsed > 0 || terminal ? formatPipelineDuration(liveElapsed) : "--",
      stage: `${authoritativeTaskIdentity.stageName} · ${eventStatus} · ${pidLabel}${failureCode ? ` · ${failureCode}` : ""}`,
      queue: eventQueue,
    };
  }, [authoritativeTaskEvent, authoritativeTaskIdentity, motionSubject, pipelineClock, selectedBrain, selectionAuthorityMode, taskRequestPending]);

  const expandedSidebar = (
    <ExpandedSidePanelShell className="cockpit-side" onToggle={() => {
      setCollapsed(true);
      execute("workspace.sidebar.collapse", "Sidebar collapsed; workspace panels remained mounted");
    }}>
      <GlassPill className="cockpit-side__new-pill" geometrySchema="side-rail" leading={<GlassIconOrb color="#69d9f5" size={36} decorative><span>+</span></GlassIconOrb>} onClick={() => openPopup("new-brain")}>New Brain</GlassPill>
      <div className="cockpit-output-root-row">
        <small className="cockpit-workspace-path" title={defaultOutputRoot || "App roaming default"}>{defaultOutputRoot || "App roaming default"}</small>
        <GlassPill geometrySchema="side-rail" leading={<CommandIdentityOrb identity="folder" />} onClick={() => openPopup("output-directory")}>Output Dir</GlassPill>
      </div>
      <div className="cockpit-local-command-row">
        <GlassPill onClick={() => openPopup("search", "workspace.search", "Local Search opened")}>Search</GlassPill>
        <GlassPill onClick={() => openPopup("pinned", "session.update", "Pinned list opened")}>Pinned</GlassPill>
      </div>
      <strong>Project Brains</strong>
      <div className="cockpit-brains">
        {brains.map((brain) => (
          <div className={`cockpit-brain-row ${editingBrain === brain ? "is-renaming" : ""}`} key={brain}>
            <GlassPill className={`${brain === committedBrain ? "is-active" : ""} ${brain === selectedBrain && brain !== committedBrain ? "is-viewing" : ""} ${editingBrain === brain ? "is-editing" : ""}`} geometrySchema="side-rail" data-brain-selection-state={brain === committedBrain ? "committed" : brain === selectedBrain ? selectionAuthorityMode.toLowerCase() : "available"} leading={<BrainOrb brain={brain} color={colorFor(brain)} />} onClick={() => editingBrain !== brain && selectBrain(brain)}>
              <span title={`${brain} · ${brainRailMeta(brain)}`}>{brain}<small>{brainRailMeta(brain)}</small></span>
            </GlassPill>
            {editingBrain === brain && <input
              className="cockpit-brain-row__rename"
              autoFocus
              value={renameValue}
              aria-label={`Rename ${brain}`}
              onClick={(event) => event.stopPropagation()}
              onChange={(event) => setRenameValue(event.target.value)}
              onBlur={() => { void commitBrainRename(); }}
              onKeyDown={(event) => {
                if (event.key === "Enter") void commitBrainRename();
                if (event.key === "Escape") { setEditingBrain(null); setRenameValue(""); }
              }}
            />}
            {pinnedBrains.includes(brain) && <span className="cockpit-brain-row__pin" aria-label="Pinned">⌖</span>}
            <button className="cockpit-brain-row__menu" data-brain-menu-trigger="true" onClick={(event) => {
              event.stopPropagation();
              const bounds = event.currentTarget.getBoundingClientRect();
              setBrainMenu((value) => value?.brain === brain ? null : {
                brain,
                left: Math.min(window.innerWidth - 202, bounds.right + 8),
                top: Math.min(window.innerHeight - 294, Math.max(12, bounds.top - 4)),
              });
            }} aria-label={`${brain} actions`}>•••</button>
          </div>
        ))}
      </div>
      <GlassPill className="cockpit-active-session" geometrySchema="side-rail" data-brain-selection-authority={selectionAuthorityMode} leading={<BrainOrb brain={committedBrain || selectedBrain} color={colorFor(committedBrain || selectedBrain)} />} trailing={<GlassIconOrb color="#69d9f5" size={28} decorative><span>+</span></GlassIconOrb>} onClick={() => execute("session.update", `${committedBrain || selectedBrain} active session selected`)}>
        <span>{committedBrain ? "Connected to" : "Viewing only"}<small>{committedBrain || `${selectedBrain} · backend unavailable`}</small></span>
      </GlassPill>
      <GlassPill className="cockpit-profile-pill" geometrySchema="side-rail" leading={profileAvatar(30)} trailing={<GlassIconOrb className="cockpit-settings-orb" color="#69d9f5" size={40} decorative><SettingsIcon /></GlassIconOrb>} onClick={() => openPopup("profile")}>
        <span>{profileName}<small>Profile &amp; Settings</small></span>
      </GlassPill>
    </ExpandedSidePanelShell>
  );

  const collapsedSidebar = (
    <CollapsedSidePanelShell
      className="cockpit-side"
      onToggle={() => {
        setCollapsed(false);
        execute("workspace.sidebar.expand", "Sidebar expanded; workspace panels remained mounted");
      }}
      onAction={sidebarAction}
    >
      <div className="cockpit-side__collapsed-bottom">
        <GlassPill iconOnly leading={<BrainOrb brain={committedBrain || selectedBrain} color={colorFor(committedBrain || selectedBrain)} />} width={44} minHeight={44} aria-label={committedBrain ? `${committedBrain} active session` : `${selectedBrain} viewing only; backend unavailable`} onClick={() => execute("session.update", `${committedBrain || selectedBrain} active session selected`)} />
        <GlassPill iconOnly leading={profileAvatar(30)} width={44} minHeight={44} aria-label={`${profileName} profile`} onClick={() => openPopup("profile")} />
      </div>
    </CollapsedSidePanelShell>
  );

  return (
      <main
        className={`cockpit-simulation t023-cockpit ${sqliteGlassTheme.className}`}
        data-evidence-theme={sqliteGlassTheme.id}
        data-evidence-module={sqliteGlassTheme.module}
        data-action-registry={evidenceActionRegistry.version}
        data-testid="t023-cockpit"
        data-active-module={activeModule}
        data-selected-brain-context={selectedBrainContext?.context_sha256 || "loading"}
        data-viewed-brain={selectedBrain}
        data-active-brain={committedBrain || "UNBOUND"}
        data-selection-authority={selectionAuthorityMode}
        style={sqliteGlassThemeStyle}
      >
        {activeModule === "telemetry3d" ? (
          <BrainTelemetry3DPage
            execute={execute}
            telemetryBundle={selectedBrainContext?.telemetry_bundle || null}
            brains={brains}
            selectedBrain={selectedBrain}
            activeBrain={committedBrain}
            selectionAuthorityMode={selectionAuthorityMode}
            brainColors={brainColors}
            onSelectBrain={selectBrain}
            onJumpSQLite={() => {
              setActiveModule("sqlite");
              void execute("session.update", "SQLite Builder module selected");
            }}
            onEscapeAdmin={() => openPopup("admin", "admin.escape", "Admin Cockpit opened")}
            railCollapsed={collapsed}
            onRailCollapsedChange={(nextCollapsed) => {
              setCollapsed(nextCollapsed);
              void execute(
                nextCollapsed ? "workspace.sidebar.collapse" : "workspace.sidebar.expand",
                `Telemetry sidebar ${nextCollapsed ? "collapsed" : "expanded"}; telemetry remained mounted`,
              );
            }}
            onRailAction={sidebarAction}
            profileName={profileName}
            profileAvatar={profileAvatar(30)}
            onOpenProfile={() => openPopup("profile")}
            reducedMotion={Boolean(reduceMotion)}
            fullViewPromptBar={(
              <SQLiteCommandBar
                onCommand={command}
                building={building}
                complete={phase === "complete"}
                openExternal
                activeBrainColor={colorFor(selectedBrain)}
              />
            )}
          />
        ) : (
        <WorkspaceShell
          className={`cockpit-frame ${collapsed ? "is-collapsed" : "is-expanded"}`}
          sideMode={collapsed ? "collapsed" : "expanded"}
          label="Evidence Lane complete frontend simulation theme"
        >
          {collapsed ? collapsedSidebar : expandedSidebar}
          <section className="cockpit-main" data-testid="cockpit-main">
            <EvidenceHeaderShell
              activeModule="sqlite"
              onModuleSwitch={(module) => {
                setActiveModule(module);
                void execute("session.update", `${module} module selected`);
              }}
              onEscapeAdmin={() => openPopup("admin", "admin.escape", "Admin Cockpit opened")}
            />
            <div className="cockpit-upper">
              <LiveMetricsClusterPanel
                className="cockpit-metrics"
                brainName={governedBrainRoute().brain_name}
                workspaceDir={governedBrainRoute().workspace_dir}
                taskEvent={authoritativeTaskEvent}
              />
              <SystemTaskStatus
                className="cockpit-task"
                progress={motionTaskModel.progress}
                status={motionTaskModel.status}
                task={motionTaskModel.task}
                detail={motionTaskModel.detail}
                running={motionTaskModel.running}
                queued={motionTaskModel.queued}
                completed={motionTaskModel.completed}
                elapsed={motionTaskModel.elapsed}
                stage={motionTaskModel.stage}
                onAction={taskAction}
                queue={motionTaskModel.queue}
                taskIdentity={authoritativeTaskIdentity}
                taskEvent={authoritativeTaskEvent}
              />
            </div>
            <section className="cockpit-tool"><ToolchainFlow
              phase={phase}
              activeIndex={toolIndex}
              brainColor={colorFor(selectedBrain)}
              brainMotionState={brainMotionState}
              motionSubject={motionSubject}
              refreshState={motionSubject === "refresh" && building ? "active" : refreshComplete || refreshReady ? "success" : "idle"}
              fuseState={motionSubject === "fuse" && building ? "active" : fuseComplete ? "success" : "idle"}
              fuseReady={refreshReady && Boolean(refreshCandidateId)}
              controlsDisabled={building}
              fuseDisabled={building || !refreshReady || !FIRST_M0_HIL_PASSED}
              taskIdentity={authoritativeTaskIdentity}
              taskEvent={authoritativeTaskEvent}
              taskEvents={authoritativeToolEvents}
              onRefresh={() => { void startGovernedToolchainAction("refresh"); }}
              onFuse={() => { void startGovernedToolchainAction("fuse"); }}
            /></section>
            <SQLiteCommandBar onCommand={command} building={building} complete={phase === "complete"} openExternal activeBrainColor={colorFor(selectedBrain)} />
          </section>
        </WorkspaceShell>
        )}

        <AnimatePresence>
            {brainMenu && <motion.div
              key={brainMenu.brain}
              className={`cockpit-brain-actions-motion ${sqliteGlassTheme.className}`}
              data-evidence-theme={sqliteGlassTheme.id}
              data-evidence-module={sqliteGlassTheme.module}
              style={{ ...sqliteGlassThemeStyle, left: brainMenu.left, top: brainMenu.top }}
              data-testid="brain-actions-motion"
              initial={false}
              animate={{ x: 0, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, scale: .97 }}
            >
              <GlassShell className="cockpit-brain-actions cockpit-popup-stack" label={`${brainMenu.brain} actions`} pillCluster="brain-actions">
              <strong>{brainMenu.brain}</strong>
              <GlassPill onClick={() => {
                setEditingBrain(brainMenu.brain);
                setRenameValue(brainMenu.brain);
                setBrainMenu(null);
              }}>Rename</GlassPill>
              <GlassPill onClick={() => toggleBrainPin(brainMenu.brain)}>{pinnedBrains.includes(brainMenu.brain) ? "Unpin" : "Pin"}</GlassPill>
              <GlassPill onClick={() => {
                const name = brainMenu.brain;
                setBrains((current) => current.filter((item) => item !== name));
                setBrainColors((current) => { const updated = { ...current }; delete updated[name]; return updated; });
                setBrainMenu(null);
                execute("brain.remove", `${name} removed from the real workspace registry`, { brain_name: name });
              }}>Remove</GlassPill>
              <GlassPill variant="danger" className="is-delete" onClick={() => {
                setActionBrain(brainMenu.brain);
                setBrainMenu(null);
                setPopup("delete-brain");
              }}>Delete to Recycle Bin</GlassPill>
              <GlassPill onClick={() => setBrainMenu(null)}>Close</GlassPill>
              </GlassShell>
            </motion.div>}
        </AnimatePresence>

        <AnimatePresence initial={false}>
            {popup && <motion.div
              key={universalPopupFadeMotion.presenceKey}
              ref={popupDialogRef}
              className={`cockpit-overlay ${sqliteGlassTheme.className}`}
              data-evidence-theme={sqliteGlassTheme.id}
              data-evidence-module={sqliteGlassTheme.module}
              data-popup-fade-schema={universalPopupFadeMotion.schemaId}
              data-popup-kind={popup}
              style={sqliteGlassThemeStyle}
              role="dialog"
              aria-modal="true"
              tabIndex={-1}
              data-testid={`popup-${popup}`}
              initial={reduceMotion ? false : universalPopupFadeMotion.initial}
              animate={universalPopupFadeMotion.animate}
              exit={universalPopupFadeMotion.exit}
              transition={reduceMotion ? universalPopupFadeMotion.reducedTransition : universalPopupFadeMotion.transition}
              onPointerDown={(event) => { if (event.target === event.currentTarget) closePopup(); }}
            >
              <div
                className={`cockpit-overlay__motion cockpit-overlay__motion--${popup} ${popup === "source-intake" ? (activeLane ? "has-active-lane" : "is-lane-selector") : ""}`.trim()}
              >
                <GlassShell surface="frosted-popup" className={`cockpit-overlay__shell cockpit-overlay__shell--universal-pill-governance cockpit-overlay__shell--${popup}`} label={`${popupTitle(popup)} governed backend UI`} pillCluster={`popup-${popup}`}>
                <header>
                  <div><small>EVIDENCE LANE · {popupReceipt ? popupReceipt.backend_connected ? "REAL BACKEND CONNECTED" : popupReceipt.mode === "backend" ? "BACKEND FAILED CLOSED" : "UI-ONLY CONTROL" : "BACKEND CHECK PENDING"}</small><h2>{popupTitle(popup)}</h2></div>
                  <GlassPill onClick={closePopup}>Close</GlassPill>
                </header>

                {popup === "ollama" && <div className="simulation-governed-popup">
                  <div className="simulation-contract-card"><strong>Ollama application unavailable or unconfigured</strong><span>Configured Ollama launches directly without this popup. This universal fallback remains only for a missing or invalid application path.</span><span>No project data was sent.</span></div>
                  <div className="simulation-button-row">
                    <GlassPill leading={<CommandIdentityOrb identity="ollama" />} onClick={() => { void launchConfiguredApplication("ollama", "ollama", "ollama.launch"); }}>Retry Ollama</GlassPill>
                    <GlassPill leading={<CommandIdentityOrb identity="telemetry" />} onClick={() => { void execute("ollama.status", "Ollama backend status refreshed").then(setPopupReceipt); }}>Refresh Status</GlassPill>
                    <GlassPill onClick={() => setPopup("settings")}>Configure in Settings</GlassPill>
                  </div>
                </div>}

                {popup === "new-brain" && <div className="simulation-new-brain">
                  <div className={`simulation-new-brain__preview is-${newBrainStage}`} style={{ "--new-brain-color": newBrainStage === "success" ? "#49d398" : newBrainPreviewColor } as CSSProperties}>
                    <PulsatingBrain size={132} color={newBrainStage === "success" ? "#49d398" : newBrainPreviewColor} active={newBrainStage !== "idle"} intensity={1.35} />
                    <strong>{newBrainStage === "success" ? "Identity validated" : newBrainStage === "settled" ? "Brain sealed" : "Unique brain identity"}</strong>
                    <small>{newBrainStage === "success" ? "Flashing green once" : newBrainPreviewColor}</small>
                  </div>
                  <div className="simulation-new-brain__form simulation-form">
                    <label><span>Brain name</span><input autoFocus disabled={newBrainStage !== "idle"} value={newBrainName} onChange={(e) => setNewBrainName(e.target.value)} placeholder="New Brain" /></label>
                    <GlassPill state={newBrainStage === "settled" ? "success" : newBrainStage === "success" ? "active" : "idle"} disabled={newBrainStage !== "idle"} onClick={createBrainFromPopup}>{newBrainStage === "success" ? "Sealing" : newBrainStage === "settled" ? "Created" : "Create Brain"}</GlassPill>
                  </div>
                </div>}

                {popup === "search" && <div className="simulation-search">
                  <label><span>Search projects, chats and brains</span><input autoFocus value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)} placeholder="Search local workspace" /></label>
                  <div className="simulation-result-list simulation-result-list--orbs simulation-result-list--search">{searchResults.map((result) => <button key={result.id} onClick={() => {
                    if (result.kind === "chat") setSelectedChatId(result.id);
                    void selectBrain(result.brain);
                    if (result.kind === "chat") void execute("chat.markRead", `${result.title} selected from search`, { brain_name: result.brain });
                  }}><BrainOrb brain={result.brain} size={40} color={colorFor(result.brain)} /><span><strong>{result.title}</strong><small>{result.meta}</small></span><BrainSignal brain={result.brain} color={colorFor(result.brain)} /><i style={{ "--result-color": colorFor(result.brain) } as CSSProperties} /></button>)}</div>
                </div>}

                {popup === "pinned" && <div className="simulation-result-list simulation-result-list--orbs">
                  <h3>Pinned Brains</h3>
                  {pinnedBrains.map((name) => <button key={name} onClick={() => selectBrain(name)}><BrainOrb brain={name} size={40} color={colorFor(name)} /><span><strong>{name}</strong><small>brain</small></span><i style={{ "--result-color": colorFor(name) } as CSSProperties} /></button>)}
                  <h3>Pinned Chats</h3>
                  {chats.filter((chat) => chat.pinned).map((chat) => <button key={chat.id} onClick={() => { void selectBrain(chat.brain); setSelectedChatId(chat.id); void execute("chat.markRead", `${chat.title} selected`, { brain_name: chat.brain }); }}><BrainOrb brain={chat.brain} size={40} color={colorFor(chat.brain)} /><span><strong>{chat.title}</strong><small>{chat.brain}</small></span><i style={{ "--result-color": colorFor(chat.brain) } as CSSProperties} /></button>)}
                </div>}

                {popup === "source-intake" && <div className="simulation-source-intake">
                  {!activeLane && <div className="cockpit-overlay__pill-grid source-lane-grid" data-universal-pill-grid="contained">{availableSourceLanes.map((lane) => <GlassPill
                    className="source-lane-pill"
                    leading={<GlassIconOrb className="source-lane-orb" color={brainColor(lane.key)} size={38} label={`${lane.label} lane`}><SourceLaneIcon lane={lane.key} /></GlassIconOrb>}
                    key={lane.key}
                    onClick={() => {
                      setActiveLaneKey(lane.key);
                      execute("source.lane.select", `${lane.label} type-correct lane selected`);
                    }}
                  ><span>{lane.label}<small>{(contextSourcesByLane[lane.key] || []).length} loaded · {lane.schema.length} lane tables</small></span></GlassPill>)}
                    <GlassPill className="source-lane-pill source-lane-pill--add-custom" leading={<GlassIconOrb color="#efca72" size={38} decorative><span>+</span></GlassIconOrb>} onClick={createCustomLane}><span>Add Custom Lane<small>Append schema-first pill</small></span></GlassPill>
                  </div>}
                  {activeLane && <div className="simulation-lane-card">
                    <div className="simulation-lane-card__head">
                      <GlassPill onClick={() => { setActiveLaneKey(""); execute("source.lane.select", "Source lane selector reopened"); }}>Back</GlassPill>
                      <GlassIconOrb className="source-lane-orb is-large" color={brainColor(activeLane.key)} size={44} label={`${activeLane.label} lane`}><SourceLaneIcon lane={activeLane.key} size={27} /></GlassIconOrb>
                      <div><strong>{activeLane.label}</strong><small>{activeLane.fileHint}</small></div>
                      <span className={`simulation-lane-status ${validatedLanes.includes(activeLane.key) ? "is-valid" : ""}`}>{validatedLanes.includes(activeLane.key) ? "validated" : "awaiting validation"}</span>
                    </div>
                    <div className="simulation-lane-layout">
                      <section className="simulation-lane-form simulation-contract-card">
                        {activeLane.kind === "github" ? <div className="simulation-lane-fields">
                          <label className="is-full"><span>Repository URL · required</span><input value={activeLaneDraft.repo_url || ""} onChange={(event) => updateLaneField("repo_url", event.target.value)} placeholder="https://github.com/org/repo.git" /></label>
                          <label><span>Branch or tag</span><input value={activeLaneDraft.branch || ""} onChange={(event) => updateLaneField("branch", event.target.value)} placeholder="default branch" /></label>
                          <label><span>Private token · memory only</span><input type="password" value={activeLaneDraft.private_token || ""} onChange={(event) => updateLaneField("private_token", event.target.value)} /></label>
                        </div> : <div className="simulation-native-source-picker">
                          <strong>Native source picker</strong>
                          <small>Windows Explorer is restricted to this lane's classified {activeLanePolicy?.extensions.length ? activeLanePolicy.extensions.join(", ") : "folder"} contract. No unrelated file type or filesystem path is exposed in this card.</small>
                          <div className="simulation-button-row">
                            {activeLanePolicy?.kind !== "folder" && <GlassPill leading={<CommandIdentityOrb identity="folder" />} onClick={() => requestNativeSourcePicker("file")}>Open Classified Files</GlassPill>}
                            {(activeLanePolicy?.kind === "folder" || activeLanePolicy?.kind === "file-or-folder") && <GlassPill leading={<CommandIdentityOrb identity="folder" />} onClick={() => requestNativeSourcePicker("folder")}>Open Folder</GlassPill>}
                          </div>
                          {activeLaneDraft.source_selection ? <span>{activeLaneDraft.source_selection}</span> : <em>Nothing selected</em>}
                        </div>}
                        <div className="simulation-lane-laws">{activeLane.laws.map((law) => <span key={law}><i />{law}</span>)}</div>
                        {activeLane.key === "local_code"
                          ? <div className="simulation-lane-laws"><span><i />One folder dialog validates and registers atomically; no second folder selection is requested.</span></div>
                          : <div className="simulation-button-row simulation-lane-actions">
                            <GlassPill onClick={validateLane}>Validate Lane</GlassPill>
                            <GlassPill variant="active-gold" onClick={addSource}>{activeLane.actionLabel}</GlassPill>
                          </div>}
                        {activeLane.key === "plan" && <section className="simulation-plan-lane-contract">
                          <strong>Canonical Plan delta and Prompt 2</strong>
                          <small>The Plan source first follows the normal Build pipeline. Its parsed result then becomes the immutable initial Plan delta; later steers append without replacing it.</small>
                          <GlassPill onClick={() => { void openPlanGoal(); }}>Open Plan Goal</GlassPill>
                        </section>}
                      </section>
                      <aside className="simulation-lane-schema">
                        <section className="simulation-contract-card">
                          <header><strong>Universal schema contract</strong><GlassPill onClick={inspectLaneSchema}>Inspect</GlassPill></header>
                          <small>Base identities</small>
                          <div className="simulation-schema-chips">{universalSourceSchema.map((table) => <span key={table}>{table}</span>)}</div>
                          <small>{activeLane.label} additions</small>
                          <div className="simulation-schema-chips is-lane">{activeLane.schema.map((table) => <span key={table}>{table}</span>)}</div>
                        </section>
                        <section className="simulation-contract-card simulation-loaded-sources">
                          <header><strong>Loaded sources</strong><span>{(contextSourcesByLane[activeLane.key] || []).length}</span></header>
                          <div className="simulation-source-list">{(contextSourcesByLane[activeLane.key] || []).length
                            ? (contextSourcesByLane[activeLane.key] || []).map((source) => <div key={source}><span>{source.split(/[\\/]/).filter(Boolean).at(-1) || source}</span><button onClick={() => { void removeSource(activeLane.key, source); }}>Drop</button></div>)
                            : <small>No registered source in this lane.</small>}
                          </div>
                          <GlassPill variant={(contextSourcesByLane[activeLane.key] || []).length ? "danger" : "disabled"} disabled={!(contextSourcesByLane[activeLane.key] || []).length} onClick={() => {
                            void removeLaneSources(activeLane.key, activeLane.label);
                          }}>Drop Loaded Lane</GlassPill>
                        </section>
                      </aside>
                    </div>
                    {schemaInspectorLaneKey === activeLane.key && <motion.section className="schema-inspector simulation-schema-inspector" initial={{ opacity: 0, y: 18, scale: 0.985 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 10, scale: 0.99 }}>
                      <header><div><strong>{activeLane.label} schema Inspector</strong><small>Change the governed lane schema. New source intake follows the saved version; prior evidence remains preserved.</small></div><GlassPill onClick={() => setSchemaInspectorLaneKey("")}>Close</GlassPill></header>
                      <label><span>One SQLite table identifier per line</span><textarea value={schemaInspectorDraft} onChange={(event) => setSchemaInspectorDraft(event.target.value)} disabled={schemaInspectorBusy} /></label>
                      <div className="simulation-schema-inspector__meta"><span>version {laneSchemaVersions[activeLane.key] || 0}</span><span>{schemaInspectorDraft.split(/\r?\n/).filter((line) => line.trim()).length} identifiers</span></div>
                      {schemaInspectorError && <span className="simulation-error">{schemaInspectorError}</span>}
                      <div className="simulation-button-row"><GlassPill disabled={schemaInspectorBusy} onClick={resetLaneSchema}>Restore Default</GlassPill><GlassPill variant="active-gold" disabled={schemaInspectorBusy || !schemaInspectorDraft.trim()} onClick={saveLaneSchema}>Save Lane Schema</GlassPill></div>
                    </motion.section>}
                  </div>}
                </div>}

                {popup === "output-directory" && <div className="simulation-output-directory simulation-form">
                  <section className="simulation-contract-card">
                    <strong>Current output directory for new brains</strong>
                    <small>Changing this folder makes it the only catalog root. Older brain folders are not deleted, moved, or rescanned; they simply leave the active rail.</small>
                    <span className="simulation-path-display">{defaultOutputRoot || "App roaming default"}</span>
                    <div className="simulation-button-row">
                      <GlassPill leading={<CommandIdentityOrb identity="folder" />} onClick={() => { void chooseOutputRoot(); }}>Set Current Output Folder</GlassPill>
                    </div>
                    {outputRootResult && <span className="simulation-success">{outputRootResult}</span>}
                  </section>
                  <section className="simulation-contract-card simulation-loaded-sources">
                    <header><strong>Current brain root</strong><span>{workspaceRoots.length}</span></header>
                    <small>Only this root is discovered at startup. Source trees are not rebuilt or reindexed until an explicit Build or Refresh action.</small>
                    <div className="simulation-source-list">{workspaceRoots.map((root) => <div key={root.path}>
                      <span>{root.displayName}{root.active ? " · Active" : ""}</span>
                      <small>{root.path} · {brainCatalog.filter((brain) => brain.rootPath.toLowerCase() === root.path.toLowerCase()).length} brain(s)</small>
                    </div>)}</div>
                  </section>
                </div>}

                {popup === "profile" && <div className="simulation-profile">
                  <div className="simulation-profile__identity">
                    {profileAvatar(38)}
                    <div><strong>{profileName}</strong><small>{profileRole}</small></div>
                  </div>
                  <div className="simulation-profile__actions">
                    <GlassPill state={profileEditing ? "active" : "idle"} tone="cyan" onClick={() => setProfileEditing((value) => !value)}>Profile</GlassPill>
                    <GlassPill onClick={() => setPopup("settings")}>Settings</GlassPill>
                  </div>
                  <AnimatePresence initial={false}>
                    {profileEditing && <motion.div
                      className="simulation-profile__editor simulation-form"
                      initial={reduceMotion ? false : { opacity: 0, y: 8, height: 0 }}
                      animate={{ opacity: 1, y: 0, height: "auto" }}
                      exit={{ opacity: 0, y: 5, height: 0 }}
                      transition={{ type: "spring", stiffness: 280, damping: 24 }}
                    >
                      <label><span>Display name</span><input value={profileName} onChange={(e) => setProfileName(e.target.value)} /></label>
                      <label><span>Profile label</span><input value={profileRole} onChange={(e) => setProfileRole(e.target.value)} /></label>
                      <div className="simulation-profile__image-picker"><span>Profile image</span><GlassPill leading={profileAvatar(30)} onClick={() => { void chooseProfileImage(); }}>{profileImageUrl ? "Change Profile Image" : "Choose Profile Image"}</GlassPill>{profileImageUrl && <GlassPill onClick={() => { if (!persistSimulationProfileState({ profileImageDataUrl: "", profileImageName: "" })) { setEvent("Profile image was not removed because persistent simulation storage is unavailable"); return; } setProfileImageUrl(""); setProfileImageName(""); execute("profile.update", "Stored profile image removed by the user", { values: { avatar_ref: null, metadata: { profile_image_name: null } } }); }}>Remove Profile Image</GlassPill>}{profileImageName && <small>{profileImageName} · stored until changed</small>}</div>
                      <GlassPill onClick={() => { if (!persistSimulationProfileState({ profileName, profileRole })) { setEvent("Profile values were not applied because persistent simulation storage is unavailable"); return; } execute("profile.update", "Profile values stored for later simulation runs", { values: { display_name: profileName, role_label: profileRole } }); setProfileEditing(false); }}>Save Profile</GlassPill>
                    </motion.div>}
                  </AnimatePresence>
                </div>}

                {popup === "settings" && <div className="simulation-settings">
                  <div className="simulation-choice-group"><span>Appearance</span><div>
                    {(["system", "light", "dark"] as const).map((value) => <GlassPill key={value} state={appearance === value ? "active" : "idle"} tone="cyan" onClick={() => { setAppearance(value); setEvent(`Local appearance preview selected: ${value}; Save Settings persists it`); }}>{value === "system" ? "Windows" : value === "light" ? "Evidence Light" : "Evidence Dark"}</GlassPill>)}
                  </div></div>
                  <div className="simulation-choice-group"><span>Accent</span><div>
                    {(["evidence", "cyan", "gold"] as const).map((value) => <GlassPill key={value} state={accent === value ? "active" : "idle"} tone={value === "gold" ? "gold" : "cyan"} onClick={() => { setAccent(value); setEvent(`Local accent preview selected: ${value}; Save Settings persists it`); }}>{value === "evidence" ? "Cyan + Gold" : value}</GlassPill>)}
                  </div></div>
                  <div className="simulation-contract-card"><strong>Theme extension contract</strong><span>Theme ID: evidence-sqlite-glass-v1</span><span>Module: sqlite-builder</span><span>Backend adapter: replaceable</span></div>
                  <div className="simulation-contract-card simulation-launch-settings">
                    <strong>Direct application launch</strong>
                    <small>Ollama may use a user-chosen Windows application. Codex always uses automatic Windows app discovery; manual Codex paths and application IDs are disabled.</small>
                    <span>Ollama application · {ollamaExecutablePath ? ollamaExecutablePath.split(/[\\/]/).filter(Boolean).at(-1) : "Windows discovery"}</span>
                    <span>Codex application · Automatic Windows discovery</span>
                    <div className="simulation-button-row">
                      <GlassPill onClick={() => { void chooseOllamaExecutable(); }}>{ollamaExecutablePath ? "Change Ollama Application" : "Choose Ollama Application"}</GlassPill>
                      {ollamaExecutablePath && <GlassPill onClick={() => { void execute("ollama.settings.update", "Ollama application override removed; Windows discovery restored", { values: { ollama_executable_override: null } }).then((receipt) => { setPopupReceipt(receipt); if (receipt.ok) setOllamaExecutablePath(""); }); }}>Use Windows Discovery</GlassPill>}
                      <GlassPill onClick={() => { void execute("codex.status", "Codex automatic Windows discovery checked").then(setPopupReceipt); }}>Check Codex App</GlassPill>
                    </div>
                  </div>
                  <div className="simulation-contract-card simulation-character-settings" data-renderer-state={t023CharacterProfileManifest.rendererState}>
                    <strong>Character Profile</strong>
                    <span>M0 and Public Version 1 use the accepted combined PNG character only.</span>
                    <span>GLB assets and controls are deferred until Public Version 2, after Installer Version 1.</span>
                    <span>{t023CharacterProfileManifest.rendererState}</span>
                  </div>
                  <div className="simulation-button-row"><GlassPill onClick={() => setPopup("profile")}>Profile</GlassPill><GlassPill onClick={() => execute("settings.update", `Settings saved: ${appearance}, ${accent}`, { category: "interface", values: { theme_mode: appearance, accent } })}>Save Settings</GlassPill></div>
                </div>}

                {popup === "admin" && <div className="simulation-contract-card simulation-future-route"><strong>Admin Cockpit route is reserved</strong><span>{t023FutureRouteStates.admin} · PLANNED_ROUTE_RESERVED_ONLY</span><span>{t023FutureRouteStates.telemetry3d} · THREE_D_BRAIN_TELEMETRY_ACTIVE_PUBLIC_V1_REAL_BACKEND</span><span>{t023FutureRouteStates.formula} · FORMULA_ENGINE_IMPLEMENTATION_DEFERRED_PUBLIC_V2</span><span>Admin remains a stable route hook; the active native 3D telemetry page is independent of this deferred cockpit.</span></div>}

                {popup === "brain-output" && <div className="simulation-output-panel">
                  <code>{brainOutputRoutes[selectedBrain] || captureBrainOutputRoute(defaultOutputRoot, selectedBrain)}\packages</code>
                  <div className="simulation-result-list simulation-result-list--command-pills">
                    <button onClick={() => execute("folder.open", "ChatGPT package inspection opened", { brain_name: selectedBrain, path: `${brainOutputRoutes[selectedBrain] || captureBrainOutputRoute(defaultOutputRoot, selectedBrain)}\\packages` })}><CommandIdentityOrb identity="chatgpt" /><span><strong>ChatGPT package</strong><small>CRC + pointers + sectors</small></span></button>
                    <button onClick={() => execute("folder.open", "Gemini provider-readable package inspection opened", { brain_name: selectedBrain, path: `${brainOutputRoutes[selectedBrain] || captureBrainOutputRoute(defaultOutputRoot, selectedBrain)}\\packages` })}><CommandIdentityOrb identity="gemini" /><span><strong>Gemini readable</strong><small>Ordinary UTF-8 corpus</small></span></button>
                    <button onClick={() => execute("folder.open", "Topology render manifest inspection opened", { brain_name: selectedBrain, path: `${brainOutputRoutes[selectedBrain] || captureBrainOutputRoute(defaultOutputRoot, selectedBrain)}\\project\\topology` })}><CommandIdentityOrb identity="telemetry" /><span><strong>Topology renders</strong><small>MMD · SVG · PNG · manifests</small></span></button>
                  </div>
                  <GlassPill onClick={() => execute("folder.open", "Packages folder opened through the independent native channel", { ...governedBrainRoute(), path: `${brainOutputRoutes[selectedBrain] || captureBrainOutputRoute(defaultOutputRoot, selectedBrain)}\\packages` })}>Open Folder</GlassPill>
                </div>}

                {popup === "codex-handoff" && <div className="simulation-codex-panel">
                  <div className="simulation-contract-card"><strong>Codex application unavailable or unconfigured</strong><span>Configured Codex launches directly without this popup. The existing package/handoff routes remain available below.</span><span>{selectedBrain} · no project data is sent by the launch action.</span></div>
                  <div className="simulation-button-row">
                    <GlassPill leading={<CommandIdentityOrb identity="codex" />} onClick={() => { void launchConfiguredApplication("codex", "codex-handoff", "codex.launch"); }}>Retry Codex</GlassPill>
                    <GlassPill onClick={() => setPopup("settings")}>Configure in Settings</GlassPill>
                    <GlassPill leading={<CommandIdentityOrb identity="codex" />} onClick={() => { void execute("brain.codexHandoff.status", "Codex handoff status refreshed", { brain_name: selectedBrain }).then(setPopupReceipt); }}>Status</GlassPill>
                    <GlassPill leading={<CommandIdentityOrb identity="codex" />} onClick={() => { void execute("brain.codexHandoff.create", "Codex handoff created through the real backend", { brain_name: selectedBrain, actor: "Evidence OS user", reason: "Codex package popup action" }).then(setPopupReceipt); }}>Create Handoff</GlassPill>
                    <GlassPill leading={<CommandIdentityOrb identity="folder" />} onClick={() => { void execute("brain.codexHandoff.openFolder", "Verified Codex handoff folder opened", { brain_name: selectedBrain }).then(setPopupReceipt); }}>Open Codex Handoff</GlassPill>
                  </div>
                </div>}

                {popup === "flash" && <div className="simulation-flash"><small>FLASH PROMPT · REAL BACKEND READ</small>{flashPrompt ? <pre>{flashPrompt}</pre> : <p>No backend prompt is available.</p>}<div className="simulation-button-row"><GlassPill disabled={!flashPrompt} onClick={() => { void navigator.clipboard.writeText(flashPrompt); void execute("flash.copy", "Complete backend flash prompt copied locally"); }}>Copy Full Prompt</GlassPill></div></div>}

                {popup === "plan-goal" && <div className="simulation-plan-goal">
                  <section className="simulation-contract-card simulation-plan-goal__prompt">
                    <header><div><strong>Prompt 2 · canonical linear goal</strong><small>Generated only from active Plan deltas. Refresh-created brain deltas remain a separate ledger.</small></div><span>{planDeltaRows.length} delta(s)</span></header>
                    {planGoalPrompt ? <pre>{planGoalPrompt}</pre> : <p>No canonical Plan prompt is available. Load a Plan source and run Build Command first.</p>}
                    <div className="simulation-button-row">
                      <GlassPill disabled={!planGoalPrompt} onClick={() => { void navigator.clipboard.writeText(planGoalPrompt); }}>Copy Prompt 2</GlassPill>
                      <GlassPill onClick={() => { void loadPlanGoalState(); }}>Refresh Ledger</GlassPill>
                    </div>
                  </section>
                  <div className="simulation-plan-goal__body">
                    <section className="simulation-plan-delta-list" aria-label="Classified Plan delta chain">
                      {planDeltaRows.map((row) => {
                        const deltaId = String(row.delta_id || "");
                        const lanes = Array.isArray(row.classified_lanes) ? row.classified_lanes.map(String).join(", ") : "unclassified";
                        return <button type="button" className={selectedPlanDelta === deltaId ? "is-selected" : ""} aria-pressed={selectedPlanDelta === deltaId} key={deltaId} onClick={() => setSelectedPlanDelta(deltaId)}>
                          <span><strong>Delta {String(row.delta_sequence || "?")} · {String(row.disposition || "UNKNOWN")}</strong><small>{String(row.delta_kind || "DELTA")} · {lanes}</small></span>
                          <small>{String(row.content || "").slice(0, 180)}</small>
                        </button>;
                      })}
                      {!planDeltaRows.length && <small>No Plan delta records returned by the selected brain.</small>}
                    </section>
                    <aside className="simulation-plan-delta-actions">
                      <strong>Classify selected delta</strong>
                      <small>Every change is appended to the disposition history; no delta row is silently deleted.</small>
                      <code>{selectedPlanDelta || "No delta selected"}</code>
                      <div className="simulation-plan-disposition-grid" data-pill-cluster-exempt="true">
                        {(["APPROVED", "SUPERSEDED", "CHANGED", "FAILED", "DROPPED"] as const).map((value) => <GlassPill key={value} disabled={!selectedPlanDelta} variant={value === "DROPPED" || value === "FAILED" ? "danger" : value === "APPROVED" ? "active-gold" : "default"} onClick={() => { void setPlanDisposition(value); }}>{value}</GlassPill>)}
                      </div>
                    </aside>
                  </div>
                  <section className="simulation-contract-card simulation-plan-steer-entry">
                    <strong>Append a classified steer delta</strong>
                    <small>Directional, hard, information, UI/UX, backend, lane, and ordinary continuing interaction are classified automatically. The current canonical chain remains intact.</small>
                    <textarea value={planSteerDraft} onChange={(event) => setPlanSteerDraft(event.target.value)} placeholder="Continue the same linear goal until HIL..." />
                    <GlassPill variant="active-gold" disabled={!planSteerDraft.trim()} onClick={() => { void appendPlanSteer(); }}>Append Delta</GlassPill>
                  </section>
                  {planDeltaActionResult && <span className={popupReceipt?.ok === false ? "simulation-error" : "simulation-success"}>{planDeltaActionResult}</span>}
                </div>}

                {popup === "version-control" && <div className="simulation-version-control">
                  <section className="simulation-version-control__stack" aria-label="Verified immutable brain versions">
                    <div className="simulation-version-list">{versions.map((version) => {
                      const selectVersion = () => {
                        setSelectedVersion(version.id);
                        setVersionDropArmed(false);
                        setVersionDropConfirmation("");
                        setVersionDropResult("");
                        setEvent(`${version.id} selected from the verified backend version list`);
                      };
                      return <div
                        className={`simulation-version-card ${selectedVersion === version.id ? "is-selected" : ""}`}
                        role="button"
                        tabIndex={0}
                        aria-pressed={selectedVersion === version.id}
                        key={version.id}
                        onClick={selectVersion}
                        onKeyDown={(event) => {
                          if (event.target !== event.currentTarget || (event.key !== "Enter" && event.key !== " ")) return;
                          event.preventDefault();
                          selectVersion();
                        }}
                      ><VersionIdentity brain={selectedBrain} version={version} color={colorFor(selectedBrain)} onCopy={() => void copyVersionHash(version)} /></div>;
                    })}{!versions.length && <div className="simulation-version-empty-stack" role="status">
                      <p>No verified immutable versions were returned by the backend.</p>
                      {Array.from({ length: 3 }, (_, index) => <div className="simulation-version-card is-empty" aria-hidden="true" key={`empty-version-slot-${index}`}>
                        <span className="simulation-version-brain-pill"><BrainOrb brain={selectedBrain} size={62} color={colorFor(selectedBrain)} /><span className="simulation-version-brain-copy"><strong>{selectedBrain}</strong><small>Awaiting verified immutable version</small></span><BrainSignal brain={selectedBrain} color={colorFor(selectedBrain)} /><i className="simulation-version-state" style={{ "--result-color": "#a9bac5" } as CSSProperties} /></span>
                        <span className="simulation-version-identity"><strong>Verified version card slot {index + 1}</strong><small>No rollback target is registered</small><code className="simulation-version-hash">SHA-256 unavailable</code></span>
                      </div>)}
                    </div>}</div>
                  </section>
                  <aside><strong>Non-destructive rollback</strong><p>The selected immutable snapshot is restored into a unique new brain. Current brain and history remain preserved.</p>{rollbackResult && <span className="simulation-success">{rollbackResult}</span>}<GlassPill onClick={() => {
                    void execute("brain.version.rollback", `${selectedVersion} rollback product created`, { ...governedBrainRoute(), version_id: selectedVersion, actor_name: "Evidence OS user", reason: "Explicit non-destructive UI rollback" }).then((receipt) => { if (receipt.ok) setRollbackResult(`Restored copy registered from ${selectedVersion}`); });
                  }} disabled={!selectedVersion || selectedVersion === versions[0]?.id}>Roll Back Brain</GlassPill>
                    <div className="simulation-version-drop-divider" />
                    <strong>Drop historical rollback</strong>
                    <p>The current brain cannot be dropped. A selected earlier rollback record moves to the Windows Recycle Bin; Evidence Lane cannot recover it after the drop. Shared immutable snapshot objects remain available to other versions.</p>
                    {selectedVersion && selectedVersion === versions[0]?.id && <span className="simulation-version-current-lock">Current version locked</span>}
                    {versionDropResult && <span className={popupReceipt?.ok === false ? "simulation-error" : "simulation-success"}>{versionDropResult}</span>}
                    {!versionDropArmed && <GlassPill
                      variant={selectedVersion && selectedVersion !== versions[0]?.id ? "danger" : "disabled"}
                      disabled={!selectedVersion || selectedVersion === versions[0]?.id}
                      onClick={() => { setVersionDropArmed(true); setVersionDropConfirmation(""); setVersionDropResult(""); }}
                    >Drop Selected Version</GlassPill>}
                    {versionDropArmed && <div className="simulation-version-drop-confirmation">
                      <strong>Move rollback record to Recycle Bin?</strong>
                      <small>Type the complete selected version ID to confirm. This does not mutate the current brain.</small>
                      <code>{selectedVersion}</code>
                      <input aria-label="Confirm historical version ID" value={versionDropConfirmation} onChange={(event) => setVersionDropConfirmation(event.target.value)} />
                      <div className="simulation-button-row">
                        <GlassPill
                          variant={versionDropConfirmation === selectedVersion ? "danger" : "disabled"}
                          disabled={versionDropConfirmation !== selectedVersion}
                          onClick={() => { void dropSelectedHistoricalVersion(); }}
                        >Move to Recycle Bin</GlassPill>
                        <GlassPill onClick={() => { setVersionDropArmed(false); setVersionDropConfirmation(""); }}>Cancel</GlassPill>
                      </div>
                    </div>}
                  </aside>
                </div>}

                {popup === "task-details" && <div className="simulation-task-details">
                  <div className="simulation-dashboard-grid simulation-dashboard-grid--compact">
                    <GlassPill><span><strong>Stage</strong><small>{motionTaskModel.stage}</small></span></GlassPill>
                    <GlassPill><span><strong>Command</strong><small>{motionTaskModel.task}</small></span></GlassPill>
                    <GlassPill><span><strong>Progress</strong><small>{motionTaskModel.progress}%</small></span></GlassPill>
                    <GlassPill><span><strong>State</strong><small>{motionTaskModel.status}</small></span></GlassPill>
                    <GlassPill><span><strong>Elapsed</strong><small>{motionTaskModel.elapsed}</small></span></GlassPill>
                  </div>
                  <div className="simulation-receipts">
                    {motionTaskModel.queue.map((item) => <div key={item.id}><strong>{item.label}</strong><span>{item.state}</span></div>)}
                  </div>
                  <div className="simulation-receipts simulation-task-event-history" data-task-event-source={selectedBrainContext?.pipeline_state?.task_event_source || (livePipeline ? "LIVE_NATIVE_EVENT_STREAM" : "UNAVAILABLE")}>
                    {authoritativeTaskHistory.length ? authoritativeTaskHistory.map((taskEvent, index) => {
                      const identity = universalTaskSurfaceIdentity(taskEvent);
                      const updatedAt = String(taskEvent.update_timestamp || taskEvent.updated_at || "");
                      const stageName = String(taskEvent.stage_name || taskEvent.stage_id || "Task event");
                      const toolName = String(taskEvent.tool_id || taskEvent.lane_id || "no tool event");
                      const eventProgress = Math.max(0, Math.min(100, Number(taskEvent.global_percent ?? taskEvent.progress) || 0));
                      const receiptHash = String(taskEvent.receipt_hash || "");
                      return <div
                        key={`${identity?.taskId || "task"}-${updatedAt}-${index}`}
                        data-task-id={identity?.taskId || ""}
                        data-run-id={identity?.runId || ""}
                        data-stage-id={identity?.stageId || ""}
                        data-tool-id={identity?.toolId || ""}
                        data-process-pid={identity?.processPid || 0}
                        data-receipt-hash={receiptHash}
                      >
                        <strong>{stageName}</strong>
                        <span>{String(taskEvent.status || "unknown")} · {toolName}</span>
                        <small>{updatedAt || "timestamp unavailable"} · Task {identity?.taskId || "unavailable"} · Run {identity?.runId || "unavailable"} · PID {identity?.processPid || "unavailable"} · Receipt {receiptHash || "pending"} · {eventProgress}%</small>
                      </div>;
                    }) : <p>No persisted backend task history is available for this brain.</p>}
                  </div>
                  <div className="simulation-receipts">{receipts.length ? receipts.map((receipt) => <div key={receipt.id}><strong>{receipt.id}</strong><span>{receipt.command}</span><small>{receipt.summary} · {receipt.mode}</small></div>) : <p>No command receipt yet.</p>}</div>
                </div>}

                {popup === "delete-brain" && <div className="simulation-delete simulation-form"><p>Type <strong>{actionBrain}</strong> to confirm the simulated Windows Recycle Bin flow.</p><label><span>Confirmation</span><input value={deleteConfirmation} onChange={(e) => setDeleteConfirmation(e.target.value)} /></label><GlassPill variant={deleteConfirmation === actionBrain ? "danger" : "disabled"} disabled={deleteConfirmation !== actionBrain} onClick={() => {
                  setBrains((current) => current.filter((item) => item !== actionBrain));
                  setPinnedBrains((current) => current.filter((item) => item !== actionBrain));
                  setBrainColors((current) => { const updated = { ...current }; delete updated[actionBrain]; return updated; });
                  execute("brain.deleteToRecycleBin", `${actionBrain} moved to the Windows Recycle Bin; deletion receipt written`, { brain_name: actionBrain, confirm_brain_name: actionBrain });
                  closePopup();
                }}>Delete to Recycle Bin</GlassPill></div>}
                </GlassShell>
              </div>
            </motion.div>}
        </AnimatePresence>
        <output>{event}</output>
        <span className="simulation-selected-chat" aria-hidden="true">{selectedChatId}</span>
      </main>
  );
}
