import type { ComponentType } from "react";
import { useEffect, useMemo, useRef, type CSSProperties } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { t023CharacterProfileManifest, t023GeometryManifest } from "../contracts/T023UiManifests";
import type {
  UniversalProcessTaskEvent,
  UniversalTaskSurfaceIdentity,
} from "../contracts/T023UniversalUiSystemSchemas";
import { toolLensMotionTiming } from "../motion/ToolLensMotionTiming";
import { DatabaseIcon, GitIcon, MediaIcon, NodeIcon, PackageIcon, PythonIcon, TerminalIcon } from "./icons";
import type { EvidenceToolIconProps } from "./icons";
import { ExecutiveScanner } from "./ExecutiveScanner";
import { GlassIconOrb } from "./GlassIconOrb";
import { PanelStage } from "./PanelStage";
import { PulsatingBrain, type BrainMotionState } from "./PulsatingBrain";
import { ToolchainShell } from "./ToolchainShell";
import { UniversalGlassPill } from "./UniversalGlassPill";
import type { UniversalPillState } from "./UniversalGlassPill";
import { UniversalPillCluster } from "./UniversalPillCluster";

export type ToolFlowPhase = "idle" | "flying-to-lens" | "scanning" | "validated" | "flying-to-brain" | "digesting" | "complete" | "failed";
export type ToolFlowSubject = "tool" | "refresh" | "fuse";
export type ToolFlowItem = { id: string; label: string; Icon: ComponentType<EvidenceToolIconProps>; color: string };
export type ToolMatrixEvent = {
  tool_id?: string;
  stage_id?: string;
  status?: string;
  update_timestamp?: string;
  updated_at?: string;
};
type ToolMatrixStatus = "dead" | "queued" | "running" | "completed" | "failed" | "skipped";

const premiumLensPlacementStyle = {
  "--lens-overlay-left": `${t023CharacterProfileManifest.premiumLensPlacement.leftPercent}%`,
  "--lens-overlay-top": `${t023CharacterProfileManifest.premiumLensPlacement.topPercent}%`,
  "--lens-overlay-height": `${t023CharacterProfileManifest.premiumLensPlacement.heightPercent}%`,
} as CSSProperties;

function PremiumLensOverlay({ src }: { src: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    let cancelled = false;
    const sourceImage = new Image();
    sourceImage.decoding = "async";
    sourceImage.onload = () => {
      if (cancelled || !canvasRef.current) return;
      const canvas = canvasRef.current;
      canvas.width = sourceImage.naturalWidth;
      canvas.height = sourceImage.naturalHeight;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) return;
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.drawImage(sourceImage, 0, 0);
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
      for (let index = 0; index < pixels.data.length; index += 4) {
        const pixel = index / 4;
        const x = pixel % canvas.width;
        const y = Math.floor(pixel / canvas.width);
        const brightness = Math.max(pixels.data[index], pixels.data[index + 1], pixels.data[index + 2]);
        const blackKeyAlpha = brightness <= 42 ? 0 : brightness >= 96 ? 1 : (brightness - 42) / 54;
        const glassX = (x - canvas.width * .35) / (canvas.width * .142);
        const glassY = (y - canvas.height * .36) / (canvas.height * .18);
        const insideClearGlass = glassX * glassX + glassY * glassY < 1;
        const reflectionAlpha = brightness <= 148 ? 0 : Math.min(.58, (brightness - 148) / 107 * .58);
        pixels.data[index + 3] = Math.round(pixels.data[index + 3] * (insideClearGlass ? reflectionAlpha : blackKeyAlpha));
      }
      context.putImageData(pixels, 0, 0);
    };
    sourceImage.src = src;
    return () => {
      cancelled = true;
      sourceImage.onload = null;
    };
  }, [src]);

  return <canvas
    ref={canvasRef}
    className="tool-flow__premium-lens-overlay"
    aria-hidden="true"
    data-source={src}
    data-asset-kind="UPLOADED_PREMIUM_LENS"
    data-geometry-slot="lens-center"
    data-hand-anchor={t023CharacterProfileManifest.magnifyingGlassHandAnchor}
    data-placement-truth={t023CharacterProfileManifest.premiumLensPlacementTruthPolicy}
    style={premiumLensPlacementStyle}
  />;
}

const handOcclusionStyle = (clipPath: string) => ({
  "--hand-occlusion-clip": clipPath,
} as CSSProperties);

function CharacterHandOcclusion({ src }: { src: string }) {
  return <span className="tool-flow__hand-occlusion-plane" aria-hidden="true">
    {t023CharacterProfileManifest.handOcclusionClips.map((clip) => <img
      key={clip.id}
      className={`tool-flow__hand-occlusion is-${clip.id}`}
      src={src}
      alt=""
      draggable={false}
      data-hand-occlusion-region={clip.id}
      data-hand-occlusion-policy={t023CharacterProfileManifest.handOcclusionPolicy}
      style={handOcclusionStyle(clip.clipPath)}
    />)}
  </span>;
}

function RefreshMotionIcon({ size = 32, color = "currentColor", className = "", title = "Refresh" }: EvidenceToolIconProps) {
  return <svg className={className} width={size} height={size} viewBox="0 0 128 128" fill="none" stroke={color} strokeWidth="10" strokeLinecap="round" strokeLinejoin="round" role="img" aria-label={title}>
    <path d="M104 42V16L92 28A47 47 0 1 0 108 72" />
    <path d="M104 16H78" />
  </svg>;
}

function FuseMotionIcon({ size = 32, color = "currentColor", className = "", title = "Fuse" }: EvidenceToolIconProps) {
  return <svg className={className} width={size} height={size} viewBox="0 0 128 128" fill="none" stroke={color} strokeWidth="9" strokeLinecap="round" strokeLinejoin="round" role="img" aria-label={title}>
    <path d="M20 30h24c18 0 24 14 24 34s6 34 24 34h16" />
    <path d="M20 98h24c18 0 24-14 24-34s6-34 24-34h16" />
    <path d="m96 18 14 12-14 12M96 86l14 12-14 12" />
  </svg>;
}

const governedSubjects: Record<Exclude<ToolFlowSubject, "tool">, ToolFlowItem> = {
  refresh: { id: "refresh", label: "Refresh", Icon: RefreshMotionIcon, color: "#43c6ea" },
  fuse: { id: "fuse", label: "Fuse", Icon: FuseMotionIcon, color: "#e2b24f" },
};

export const evidenceToolFlow: ToolFlowItem[] = [
  { id: "source", label: "Source", Icon: PythonIcon, color: "#4aa8e8" },
  { id: "sqlite", label: "SQLite", Icon: DatabaseIcon, color: "#69c7ff" },
  { id: "lanes", label: "Lanes", Icon: TerminalIcon, color: "#92a9c7" },
  { id: "hashes", label: "Hashes", Icon: PackageIcon, color: "#e6b75e" },
  { id: "topology", label: "Topology", Icon: PackageIcon, color: "#63d7e9" },
  { id: "render", label: "Render", Icon: NodeIcon, color: "#63c98c" },
  { id: "chatgpt-package", label: "ChatGPT", Icon: PackageIcon, color: "#43c6ea" },
  { id: "gemini-package", label: "Gemini", Icon: PackageIcon, color: "#e2b24f" },
  { id: "validate", label: "Validate", Icon: TerminalIcon, color: "#61c887" },
  { id: "version", label: "Version", Icon: DatabaseIcon, color: "#b690d8" },
];

export const evidenceToolMatrixCatalog: ToolFlowItem[] = [
  { id: "git", label: "Git", Icon: GitIcon, color: "#f06f58" },
  { id: "python", label: "Python", Icon: PythonIcon, color: "#4aa8e8" },
  { id: "sqlite", label: "SQLite", Icon: DatabaseIcon, color: "#69c7ff" },
  { id: "node", label: "Node", Icon: NodeIcon, color: "#63c98c" },
  { id: "package", label: "Package", Icon: PackageIcon, color: "#e6b75e" },
  { id: "terminal", label: "Terminal", Icon: TerminalIcon, color: "#92a9c7" },
  { id: "mmdc", label: "MMDC", Icon: MediaIcon, color: "#63d7e9" },
  { id: "tesseract", label: "Tesseract", Icon: TerminalIcon, color: "#78a8d8" },
  { id: "npm", label: "NPM", Icon: NodeIcon, color: "#d45e64" },
  { id: "pyinstaller", label: "PyInstaller", Icon: PackageIcon, color: "#7caee4" },
  { id: "openpyxl", label: "OpenPyXL", Icon: DatabaseIcon, color: "#61c887" },
  { id: "pypdf", label: "PyPDF", Icon: MediaIcon, color: "#b690d8" },
];

const TOOL_MATRIX_VISIBLE_CAPACITY = 12;
const TOOL_ID_ALIASES: Record<string, string> = {
  mermaid: "mmdc",
  mermaid_cli: "mmdc",
  git_cli: "git",
  nodejs: "node",
  node_js: "node",
  npm_cli: "npm",
  python_runtime: "python",
  package_compiler: "package",
  provider_package: "package",
};

function canonicalToolId(value: unknown) {
  const normalized = String(value || "").trim().toLowerCase().replaceAll(/[^a-z0-9]+/g, "_").replaceAll(/^_+|_+$/g, "");
  return TOOL_ID_ALIASES[normalized] || normalized;
}

function dynamicToolItem(toolId: string): ToolFlowItem {
  const words = toolId.split("_").filter(Boolean);
  const label = words.map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(" ") || "Tool";
  return { id: toolId, label, Icon: TerminalIcon, color: "#74cfea" };
}

function toolMatrixState(
  events: ToolMatrixEvent[],
  taskStatus: string,
) {
  const states = new Map<string, ToolMatrixStatus>();
  let runningToolId = "";
  for (const event of events) {
    const toolId = canonicalToolId(event.tool_id);
    const status = String(event.status || "queued").toLowerCase();
    if (toolId && runningToolId && toolId !== runningToolId && states.get(runningToolId) === "running") {
      states.set(runningToolId, "completed");
    }
    if (!toolId) continue;
    if (status === "failed" || status === "cancelled") states.set(toolId, "failed");
    else if (status === "completed" || status === "hil_waiting") states.set(toolId, "completed");
    else if (status === "skipped") states.set(toolId, "skipped");
    else if (status === "running") states.set(toolId, "running");
    else states.set(toolId, "queued");
    if (status === "running") runningToolId = toolId;
  }
  if (["completed", "hil_waiting"].includes(taskStatus) && runningToolId && states.get(runningToolId) === "running") {
    states.set(runningToolId, "completed");
  }
  if (["failed", "cancelled"].includes(taskStatus) && runningToolId && states.get(runningToolId) === "running") {
    states.set(runningToolId, "failed");
  }
  return states;
}

export type ToolchainFlowProps = {
  phase: ToolFlowPhase;
  activeIndex: number;
  tools?: ToolFlowItem[];
  onBuild?: () => void;
  brainColor?: string;
  brainMotionState?: BrainMotionState;
  motionSubject?: ToolFlowSubject;
  refreshState?: UniversalPillState;
  fuseState?: UniversalPillState;
  fuseReady?: boolean;
  controlsDisabled?: boolean;
  fuseDisabled?: boolean;
  onRefresh?: () => void;
  onFuse?: () => void;
  taskIdentity?: UniversalTaskSurfaceIdentity | null;
  taskEvent?: Partial<UniversalProcessTaskEvent> | null;
  taskEvents?: ToolMatrixEvent[];
};

export function ToolchainFlow({
  phase,
  activeIndex,
  tools = evidenceToolFlow,
  onBuild,
  brainColor = "#67d9f6",
  brainMotionState = "idle",
  motionSubject = "tool",
  refreshState = "idle",
  fuseState = "idle",
  fuseReady = false,
  controlsDisabled = false,
  fuseDisabled = true,
  onRefresh,
  onFuse,
  taskIdentity = null,
  taskEvent = null,
  taskEvents = [],
}: ToolchainFlowProps) {
  const reduceMotion = useReducedMotion();
  const authoritativeTaskBound = Boolean(taskIdentity?.taskId && taskIdentity?.runId);
  const authoritativeTaskInFlight = authoritativeTaskBound && ["queued", "running"].includes(String(taskIdentity?.status || ""));
  const toolStates = useMemo(
    () => toolMatrixState(taskEvents, String(taskIdentity?.status || "")),
    [taskEvents, taskIdentity?.status],
  );
  const eventToolIds = useMemo(() => Array.from(new Set(taskEvents
    .map((event) => canonicalToolId(event.tool_id))
    .filter(Boolean))), [taskEvents]);
  const allMatrixTools = useMemo(() => {
    const catalogIds = new Set(evidenceToolMatrixCatalog.map((tool) => tool.id));
    return [
      ...evidenceToolMatrixCatalog,
      ...eventToolIds.filter((toolId) => !catalogIds.has(toolId)).map(dynamicToolItem),
    ];
  }, [eventToolIds]);
  const visibleMatrixTools = allMatrixTools.length <= TOOL_MATRIX_VISIBLE_CAPACITY
    ? allMatrixTools
    : allMatrixTools.slice(allMatrixTools.length - TOOL_MATRIX_VISIBLE_CAPACITY);
  const authoritativeToolId = canonicalToolId(taskIdentity?.toolId);
  const authoritativeTool = allMatrixTools.find((tool) => tool.id === authoritativeToolId);
  const active = motionSubject === "tool"
    ? authoritativeTaskInFlight && authoritativeTool
      ? authoritativeTool
      : tools[Math.max(0, Math.min(activeIndex, tools.length - 1))]
    : governedSubjects[motionSubject];
  const MovingIcon = active.Icon;
  const brainActive = phase === "flying-to-brain" || phase === "digesting" || brainMotionState !== "idle";
  const lensLockedByTask = authoritativeTaskInFlight;
  const lensHolding = lensLockedByTask || phase === "scanning" || phase === "validated";
  const completionGreen = ["validated", "flying-to-brain", "digesting", "complete"].includes(phase);
  // M0/Public V1 uses the governed combined PNG and uploaded premium lens as
  // one coordinate-matched visual stack. GLB assets and their controls are
  // explicitly deferred until Public Version 2, after Installer V1. CSS
  // mirroring and recreated lens art are forbidden; moving tool icons still
  // pass through the locked lens centre.
  // Every tool starts again at the left dock, pauses exactly in the lens, and
  // then continues to the brain; no tool may reverse out of the brain.
  // Container-query units keep the flight path tied to the real Toolchain
  // field while allowing Framer Motion to animate only compositor transforms.
  // Animating left/top here forced a full layout pass on every Chrome frame.
  const lensCenter = { x: "calc(50cqw - 20.832cqh - 50%)", y: "calc(36.25cqh - 50%)" };
  if (lensCenter.x !== t023GeometryManifest.lensCenterX || lensCenter.y !== t023GeometryManifest.lensCenterY) {
    throw new Error("T023_GEOMETRY_MANIFEST_LENS_MISMATCH");
  }
  const movingTarget = lensLockedByTask
    ? { ...lensCenter, opacity: 1, scale: 1.06, rotateZ: 0 }
    : phase === "flying-to-lens"
    ? { ...lensCenter, opacity: 1, scale: 1, rotateZ: -2 }
    : phase === "scanning"
      ? { ...lensCenter, opacity: 1, scale: [1, 1.1, 1], rotateZ: [-2, 2, -2] }
      : phase === "validated"
        ? { ...lensCenter, opacity: 1, scale: 1.06, rotateZ: 0 }
        : phase === "flying-to-brain"
          ? { x: [lensCenter.x, "calc(72cqw - 50%)", "calc(91cqw - 50%)"], y: [lensCenter.y, "calc(42cqh - 50%)", "calc(67cqh - 50%)"], opacity: [1, 1, .72], scale: [1.06, .9, .44], rotateZ: [0, 8, 0] }
          : { x: "calc(91cqw - 50%)", y: "calc(67cqh - 50%)", opacity: 0, scale: .08, rotateZ: 10 };
  const movingTransition = lensLockedByTask
    ? { duration: toolLensMotionTiming.validatedHandoffSeconds, ease: "linear" as const }
    : reduceMotion
    ? { duration: toolLensMotionTiming.reducedMotionSeconds }
    : phase === "flying-to-lens"
      ? { duration: toolLensMotionTiming.toolToLensSeconds, ease: toolLensMotionTiming.toolToLensEase }
      : phase === "scanning"
      ? { duration: 1.25, repeat: Infinity, ease: "easeInOut" as const }
      : phase === "validated"
        ? { duration: toolLensMotionTiming.validatedHandoffSeconds, ease: "linear" as const }
      : phase === "flying-to-brain"
        ? { duration: toolLensMotionTiming.lensToBrainSeconds, times: toolLensMotionTiming.lensToBrainTimes, ease: toolLensMotionTiming.lensToBrainEase }
        : { type: "spring" as const, stiffness: 126, damping: 22, mass: .9 };
  return <ToolchainShell className={`tool-flow-shell is-${phase}`} label="Evidence Lane toolchain flow">
    <PanelStage className="tool-flow-stage" label="Centered coded Toolchain stage">
      <div
        className="tool-flow__dock"
        aria-label="Authoritative tool event matrix"
        data-matrix-schema="T023_DYNAMIC_FOUR_COLUMN_TOOL_EVENT_MATRIX_V1"
        data-visible-capacity={TOOL_MATRIX_VISIBLE_CAPACITY}
        data-overflow-policy="NEWEST_EVENT_ENTERS_OLDEST_VISIBLE_LEAVES"
      >{visibleMatrixTools.map(({ id, label, Icon, color }) => {
        const state = toolStates.get(id) || "dead";
        return <div
          className={`tool-flow__dock-item is-${state}`}
          key={id}
          style={{ "--tool-color": color } as React.CSSProperties}
          data-tool-id={id}
          data-tool-status={state}
          data-status-authority="BACKEND_TOOL_EVENT_ONLY"
        ><Icon size={30} /><span>{label}</span></div>;
      })}</div>
      <div
        className="tool-flow__cinema"
        data-geometry-manifest="T023_GEOMETRY_MANIFEST_V001"
        data-inside-lens={lensHolding ? "true" : "false"}
        data-lens-state={phase === "validated" ? "complete" : lensHolding ? "holding" : "transit"}
        data-lens-lock={lensLockedByTask ? "ACTIVE_TASK_UNTIL_TERMINAL" : "RELEASED"}
        data-task-lens-law="ACTIVE_TOOL_DEAD_CENTER_UNTIL_SYSTEM_TASK_TERMINAL"
        data-completion-green={completionGreen ? "true" : "false"}
        data-motion-subject={motionSubject}
        data-task-event-schema={taskIdentity?.schema || "UNAVAILABLE"}
        data-task-id={taskIdentity?.taskId || ""}
        data-run-id={taskIdentity?.runId || ""}
        data-request-id={taskIdentity?.requestId || ""}
        data-brain-id={taskIdentity?.brainId || ""}
        data-lane-id={taskIdentity?.laneId || ""}
        data-stage-id={taskIdentity?.stageId || ""}
        data-tool-id={taskIdentity?.toolId || ""}
        data-process-pid={taskIdentity?.processPid || 0}
        data-process-tree-ids={taskIdentity?.processTreeIds.join(",") || ""}
        data-task-status={taskIdentity?.status || "unavailable"}
        data-receipt-hash={String(taskEvent?.receipt_hash || "")}
        data-task-event-updated-at={String(taskEvent?.update_timestamp || taskEvent?.updated_at || "")}
      >
        <div
          className="tool-flow__scanner"
          data-geometry-slot="character"
          data-hand-anchor={t023CharacterProfileManifest.magnifyingGlassHandAnchor}
          data-lens-center-x={t023GeometryManifest.lensCenterX}
          data-lens-center-y={t023GeometryManifest.lensCenterY}
        ><ExecutiveScanner size="100%" /></div>
        <PremiumLensOverlay src={t023CharacterProfileManifest.premiumMagnifyingGlassAsset} />
        <CharacterHandOcclusion src={t023CharacterProfileManifest.temporaryCombinedCharacterAsset} />
        <UniversalPillCluster clusterId="toolchain-governance" className="tool-flow__governance-actions" data-geometry-slot="governance-top-right" role="group" aria-label="Refresh Brain and explicit Fuse controls">
          <UniversalGlassPill className="tool-flow__refresh" leading={<GlassIconOrb color="#69d9f5" size={36} decorative><RefreshMotionIcon size={21} /></GlassIconOrb>} state={refreshState} disabled={controlsDisabled || !onRefresh} onClick={onRefresh} aria-label="Refresh Brain through the Toolchain lens">Refresh</UniversalGlassPill>
          <UniversalGlassPill className={`tool-flow__fuse ${fuseReady ? "is-ready-vibrating" : ""}`} data-ready-vibrating={fuseReady ? "true" : "false"} leading={<GlassIconOrb color="#e5bd62" size={36} decorative><FuseMotionIcon size={21} /></GlassIconOrb>} state={fuseState} tone="gold" disabled={controlsDisabled || fuseDisabled || !onFuse} onClick={onFuse} aria-label="Fuse the ready Refresh candidate through the Toolchain lens">Fuse</UniversalGlassPill>
        </UniversalPillCluster>
        <div className="tool-flow__brain" data-geometry-slot="brain" data-brain-motion-state={brainMotionState} style={{ "--brain-orb-color": brainColor } as React.CSSProperties}><PulsatingBrain size="100%" color={brainColor} active={brainActive} intensity={1.2} motionState={brainMotionState} /></div>{authoritativeTaskBound && (authoritativeTaskInFlight || (phase !== "idle" && phase !== "complete")) && <motion.div
        key={active.id}
        className={`tool-flow__moving-icon is-${phase}`}
        style={{ "--tool-color": active.color } as React.CSSProperties}
        initial={reduceMotion ? false : { x: "calc(18cqw - 0cqh - 50%)", y: lensCenter.y, opacity: .45, scale: .72, rotateZ: -8 }}
        animate={movingTarget}
        transition={movingTransition}
        aria-label={`${active.label} ${phase}`}
      ><MovingIcon size={76} /><span>{active.label}</span></motion.div>}
      </div>
      <footer className="tool-flow__footer"><div><strong>{phase === "idle" ? "Validation Ready" : phase === "complete" ? "Toolchain Complete" : active.label}</strong><span>{phase.replaceAll("-", " ")}</span></div>{onBuild && <UniversalGlassPill onClick={onBuild} disabled={!["idle", "complete"].includes(phase)}>Build Command</UniversalGlassPill>}</footer>
    </PanelStage>
  </ToolchainShell>;
}
