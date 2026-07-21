export type ToolLensMotionPhase =
  | "idle"
  | "flying-to-lens"
  | "scanning"
  | "validated"
  | "flying-to-brain"
  | "digesting"
  | "complete"
  | "failed";

export const toolLensMotionTiming = Object.freeze({
  schemaId: "T023_TOOL_LENS_MOTION_TIMING_V001",
  toolToLensMs: 1800,
  toolToLensSeconds: 1.8,
  toolToLensEase: [0.18, 0.82, 0.28, 1] as [number, number, number, number],
  scanningFallbackMs: 1800,
  validatedHandoffMs: 96,
  validatedHandoffSeconds: 0.096,
  lensToBrainMs: 1450,
  lensToBrainSeconds: 1.45,
  lensToBrainTimes: [0, 0.48, 1] as [number, number, number],
  lensToBrainEase: [0.22, 0.76, 0.3, 1] as [number, number, number, number],
  digestingMs: 450,
  completionSettleMs: 250,
  reducedMotionMs: 1,
  reducedMotionSeconds: 0.001,
});

export const toolLensPhaseDelayMs: Partial<Record<ToolLensMotionPhase, number>> = Object.freeze({
  "flying-to-lens": toolLensMotionTiming.toolToLensMs,
  scanning: toolLensMotionTiming.scanningFallbackMs,
  validated: toolLensMotionTiming.validatedHandoffMs,
  "flying-to-brain": toolLensMotionTiming.lensToBrainMs,
  digesting: toolLensMotionTiming.digestingMs,
});
