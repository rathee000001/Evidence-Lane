import { EvidenceOSLogo } from "./EvidenceOSLogo";
import { GlassIconOrb } from "./GlassIconOrb";
import { HeaderShell } from "./HeaderShell";
import { UniversalGlassPill } from "./UniversalGlassPill";
import { UniversalPillCluster } from "./UniversalPillCluster";

function HeaderIdentityOrb({ identity }: { identity: "sqlite" | "telemetry3d" | "admin" }) {
  const color = identity === "sqlite" ? "#59d0ee" : identity === "telemetry3d" ? "#73d9f6" : "#efca72";
  return (
    <GlassIconOrb className={`header-identity-orb header-identity-orb--${identity}`} color={color} size={36} decorative>
      {identity === "sqlite" ? (
        <svg viewBox="0 0 32 32">
          <ellipse cx="16" cy="7" rx="10" ry="4" />
          <path d="M6 7v9c0 2.2 4.5 4 10 4s10-1.8 10-4V7" />
          <path d="M6 16v9c0 2.2 4.5 4 10 4s10-1.8 10-4v-9" />
        </svg>
      ) : identity === "telemetry3d" ? (
        <svg viewBox="0 0 32 32">
          <circle cx="16" cy="16" r="4" />
          <circle cx="7" cy="9" r="2.5" />
          <circle cx="25" cy="9" r="2.5" />
          <circle cx="8" cy="24" r="2.5" />
          <circle cx="25" cy="23" r="2.5" />
          <path d="m9 10.5 4.5 3.5M23 10.5 18.5 14M10 22.5l3.5-4M22.5 21.5 18.5 18" />
        </svg>
      ) : (
        <svg viewBox="0 0 32 32">
          <path d="M4 15 28 4 20 28l-5-9Z" />
          <path d="m15 19 13-15" />
        </svg>
      )}
    </GlassIconOrb>
  );
}

export type EvidenceHeaderShellProps = {
  activeModule: "sqlite" | "telemetry3d";
  onModuleSwitch: (module: "sqlite" | "telemetry3d") => void;
  onEscapeAdmin?: () => void;
};

export function EvidenceHeaderShell({ activeModule, onModuleSwitch, onEscapeAdmin }: EvidenceHeaderShellProps) {
  const activeLabel = activeModule === "sqlite" ? "SQLite Builder" : "3D Telemetry";
  const switchModule = activeModule === "sqlite" ? "telemetry3d" : "sqlite";
  const switchLabel = switchModule === "sqlite" ? "SQLite Builder" : "3D Telemetry";
  return (
    <HeaderShell className="evidence-header-shell" label="Evidence Lane command header">
      <div className="evidence-header-shell__logo">
        <div className="evidence-header-shell__logo-pane">
          <EvidenceOSLogo animated />
        </div>
      </div>
      <UniversalPillCluster
        className="evidence-header-shell__commands"
        clusterId="container-one-header-pills"
        data-container-placement-rule="T023_CONTAINER_1_HEADER_PLACEMENT_V1"
      >
        <div className="evidence-header-shell__command-grid">
          <div className="evidence-header-shell__module-pair" data-container-placement-group="centered-between-logo-and-admin">
            <UniversalGlassPill data-universal-pill-container="module-route" data-container-placement-slot="active" className="evidence-header-shell__active" variant="active-cyan" onClick={() => onModuleSwitch(activeModule)} leading={<HeaderIdentityOrb identity={activeModule} />}>
              Active Pill — {activeLabel}
            </UniversalGlassPill>
            <UniversalGlassPill data-universal-pill-container="module-route" data-container-placement-slot="module-switch" className="evidence-header-shell__switch" onClick={() => onModuleSwitch(switchModule)} leading={<HeaderIdentityOrb identity={switchModule} />}>
              {switchLabel}
            </UniversalGlassPill>
          </div>
          <UniversalGlassPill data-container-placement-slot="admin" className="evidence-header-shell__escape" onClick={onEscapeAdmin} leading={<HeaderIdentityOrb identity="admin" />}>
            Escape to Admin Cockpit
          </UniversalGlassPill>
        </div>
      </UniversalPillCluster>
    </HeaderShell>
  );
}
