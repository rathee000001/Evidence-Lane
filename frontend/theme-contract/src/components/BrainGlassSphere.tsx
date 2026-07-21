import type { CSSProperties } from "react";
import { t023UiSystemSchemas } from "../contracts/T023UniversalUiSystemSchemas";

export type BrainGlassSphereProps = { size?: number | string; className?: string; color?: string };
type BrainSphereStyle = CSSProperties & { "--brain-orb-color"?: string };

export function BrainGlassSphere({ size = 360, className = "", color = "#67d9f6" }: BrainGlassSphereProps) {
  const style: BrainSphereStyle = { width: size, height: size, "--brain-orb-color": color };
  return (
    <div
      className={`brain-sphere ${className}`.trim()}
      style={style}
      data-universal-brain-orb-schema={t023UiSystemSchemas.universalBrainOrb}
      data-brain-orb-pulse-policy="COMMITTED_SELECTED_BRAIN_ONLY"
    >
      <span className="brain-sphere__rear" aria-hidden="true" />
      <img className="brain-sphere__brain" src="/assets/evidence-static-brain.png" alt="Static Evidence Lane brain" />
      <span className="brain-sphere__glass" aria-hidden="true" />
      <span className="brain-sphere__rim" aria-hidden="true" />
      <span className="brain-sphere__reflection" aria-hidden="true" />
    </div>
  );
}
