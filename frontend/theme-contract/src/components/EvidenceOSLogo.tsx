import { memo } from "react";
import leftCore from "../generated/logo/left_core.svg";
import energyRoutes from "../generated/logo/energy_routes.svg";
import sCore from "../generated/logo/s_core.svg";

export type EvidenceOSLogoProps = { width?: number | string; className?: string; animated?: boolean };

function EvidenceOSLogoComponent({ width = "100%", className = "", animated = true }: EvidenceOSLogoProps) {
  return (
    <div className={`header-evidence-logo ${animated ? "is-animated" : ""} ${className}`.trim()} style={{ width }} role="img" aria-label="Evidence OS">
      <svg viewBox="0 425 2440 950" preserveAspectRatio="xMinYMid meet">
        <image className="header-logo__energy" href={energyRoutes} x="0" y="0" width="2400" height="1792" />
        <image className="header-logo__left" href={leftCore} x="0" y="0" width="2400" height="1792" />
        <text className="header-logo__wordmark-legacy" x="1016" y="982" textLength="900" lengthAdjust="spacingAndGlyphs">EVIDENCE</text>
        <text className="header-logo__wordmark-text" x="1010" y="930" textLength="900" lengthAdjust="spacingAndGlyphs">EVIDENCE</text>
        <image className="header-logo__s-core" href={sCore} x="0" y="0" width="2400" height="1792" />
      </svg>
    </div>
  );
}

export const EvidenceOSLogo = memo(EvidenceOSLogoComponent);
