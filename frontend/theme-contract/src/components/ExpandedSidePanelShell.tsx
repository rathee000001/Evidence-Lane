import type { ReactNode } from "react";
import { CubeAppIcon } from "./CubeAppIcon";
import { GlassShell } from "./GlassShell";
import { SideActionOrb } from "./SideActionOrb";

export type ExpandedSidePanelShellProps = {
  children?: ReactNode;
  className?: string;
  onToggle?: () => void;
};

export function ExpandedSidePanelShell({ children, className = "", onToggle }: ExpandedSidePanelShellProps) {
  return (
    <GlassShell className={`expanded-side-shell ${className}`.trim()} label="Expanded Evidence Lane side panel">
      <header className="expanded-side-shell__top">
        <CubeAppIcon className="side-app-icon" size={52} />
        <button className="side-panel-toggle" onClick={onToggle} aria-label="Collapse sidebar"><SideActionOrb action="collapse" size={38} /></button>
      </header>
      {children ?? <span className="expanded-side-shell__empty-field" aria-hidden="true" />}
    </GlassShell>
  );
}
