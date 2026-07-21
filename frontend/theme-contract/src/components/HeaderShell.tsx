import type { ReactNode } from "react";
import { GlassShell } from "./GlassShell";

export function HeaderShell({ children, className = "", label = "Evidence Lane header" }: { children?: ReactNode; className?: string; label?: string }) {
  return <GlassShell className={`header-shell ${className}`.trim()} label={label}>{children}</GlassShell>;
}
