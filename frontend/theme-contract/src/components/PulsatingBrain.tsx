import type { CSSProperties } from "react";
import { BrainGlassSphere } from "./BrainGlassSphere";

export type BrainMotionState = "idle" | "refresh-pulse" | "fuse-grow-pulse" | "success-flash";

export type PulsatingBrainProps = {
  size?: number | string;
  active?: boolean;
  intensity?: number;
  className?: string;
  color?: string;
  motionState?: BrainMotionState;
};

type PulseStyle = CSSProperties & { "--brain-intensity": number; "--brain-orb-color": string };

export function PulsatingBrain({ size = 380, active = true, intensity = 1, className = "", color = "#67d9f6", motionState = "idle" }: PulsatingBrainProps) {
  const style: PulseStyle = { width: size, height: size, "--brain-intensity": Math.max(.35, Math.min(intensity, 1.8)), "--brain-orb-color": color };
  return (
    <div
      className={`pulsating-brain ${active || motionState !== "idle" ? "is-active" : ""} is-${motionState} ${className}`.trim()}
      data-brain-motion-state={motionState}
      style={style}
    >
      <svg className="pulsating-brain__energy" viewBox="0 0 400 400" aria-hidden="true">
        <g className="pulsating-brain__arcs pulsating-brain__arcs--cool">
          <path d="M82 92a158 158 0 0 1 76-43" />
          <path d="M69 76a178 178 0 0 1 82-47" />
          <path d="M82 308a158 158 0 0 0 76 43" />
          <path d="M69 324a178 178 0 0 0 82 47" />
        </g>
        <g className="pulsating-brain__arcs pulsating-brain__arcs--warm">
          <path d="M242 49a158 158 0 0 1 76 43" />
          <path d="M249 29a178 178 0 0 1 82 47" />
          <path d="M242 351a158 158 0 0 0 76-43" />
          <path d="M249 371a178 178 0 0 0 82-47" />
        </g>
        <path className="pulsating-brain__wave pulsating-brain__wave--cool" d="M0 200h18l8-19 10 58 13-91 11 88 13-56 10 20h22" />
        <path className="pulsating-brain__wave pulsating-brain__wave--warm" d="M295 200h22l10-20 13 56 11-88 13 91 10-58 8 19h18" />
      </svg>
      <span className="pulsating-brain__halo" aria-hidden="true" />
      <BrainGlassSphere size="67%" color={color} className="pulsating-brain__orb" />
    </div>
  );
}
