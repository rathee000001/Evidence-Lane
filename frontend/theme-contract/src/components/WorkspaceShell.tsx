import type { ReactNode } from "react";
import { GlassShell } from "./GlassShell";

export type WorkspaceShellProps = {
  children?: ReactNode;
  sideMode?: "expanded" | "collapsed";
  className?: string;
  label?: string;
};

export function WorkspaceShell({ children, sideMode = "collapsed", className = "", label = "Evidence Lane workspace" }: WorkspaceShellProps) {
  return <GlassShell className={`workspace-shell workspace-shell--${sideMode} ${className}`.trim()} label={label}>{children}</GlassShell>;
}
