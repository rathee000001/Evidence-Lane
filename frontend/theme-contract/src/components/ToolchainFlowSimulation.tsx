import { useEffect, useState } from "react";
import { toolLensPhaseDelayMs } from "../motion/ToolLensMotionTiming";
import { evidenceToolFlow, ToolchainFlow, type ToolFlowPhase } from "./ToolchainFlow";

const next: Partial<Record<ToolFlowPhase, ToolFlowPhase>> = { "flying-to-lens": "scanning", scanning: "validated", validated: "flying-to-brain", "flying-to-brain": "digesting" };

export function ToolchainFlowSimulation() {
  const [phase, setPhase] = useState<ToolFlowPhase>("idle");
  const [activeIndex, setActiveIndex] = useState(0);
  useEffect(() => {
    const delay = toolLensPhaseDelayMs[phase];
    if (!delay) return;
    const timer = window.setTimeout(() => {
      if (phase === "digesting") {
        if (activeIndex >= evidenceToolFlow.length - 1) setPhase("complete");
        else { setActiveIndex((value) => value + 1); setPhase("flying-to-lens"); }
      } else setPhase(next[phase] ?? "idle");
    }, delay);
    return () => window.clearTimeout(timer);
  }, [phase, activeIndex]);
  const start = () => { setActiveIndex(0); setPhase("flying-to-lens"); };
  return <main className="tool-flow-demo"><ToolchainFlow phase={phase} activeIndex={activeIndex} onBuild={start} /><p>Dummy timing only. Backend may drive phase and activeIndex directly.</p></main>;
}
