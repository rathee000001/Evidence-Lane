import type { ReactNode } from "react";
import { CubeAppIcon } from "./CubeAppIcon";
import { GlassShell } from "./GlassShell";
import { SideActionOrb } from "./SideActionOrb";
import type { SideCommandAction } from "./SideActionOrb";

export type CollapsedSidePanelShellProps = {
  children?: ReactNode;
  className?: string;
  onToggle?: () => void;
  onAction?: (action: SideCommandAction) => void;
};

export function CollapsedSidePanelShell({ children, className = "", onToggle, onAction }: CollapsedSidePanelShellProps) {
  return (
    <GlassShell className={`collapsed-side-shell ${className}`.trim()} label="Collapsed Evidence Lane side panel">
      <header className="collapsed-side-shell__top">
        <CubeAppIcon className="side-app-icon" size={52} />
        <button className="side-panel-toggle" onClick={onToggle} aria-label="Expand sidebar"><SideActionOrb action="expand" size={38} /></button>
      </header>
      <nav className="side-action-stack" aria-label="Collapsed sidebar actions">
        {(["add", "search", "pin"] as SideCommandAction[]).map((action) => <button key={action} onClick={() => onAction?.(action)} aria-label={action}><SideActionOrb action={action} /></button>)}
      </nav>
      {children}
    </GlassShell>
  );
}
