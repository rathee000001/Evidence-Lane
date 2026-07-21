import type { ReactNode } from "react";
import { GlassShell } from "./GlassShell";

export function MetricsShell({ children, className = "", label = "Process metrics cluster" }: { children?: ReactNode; className?: string; label?: string }) {
  return <GlassShell surface="transparent" className={`metrics-shell ${className}`.trim()} label={label} data-shared-rear-glass="pc-cluster">{children}</GlassShell>;
}
