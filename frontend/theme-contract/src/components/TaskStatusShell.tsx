import type { ReactNode } from "react";
import { GlassShell } from "./GlassShell";

export function TaskStatusShell({ children, className = "", label = "System task status" }: { children?: ReactNode; className?: string; label?: string }) {
  return <GlassShell className={`task-status-shell ${className}`.trim()} label={label}>{children}</GlassShell>;
}
