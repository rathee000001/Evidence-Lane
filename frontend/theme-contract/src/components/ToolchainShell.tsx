import type { ReactNode } from "react";
import { GlassShell } from "./GlassShell";

export function ToolchainShell({ children, className = "", label = "Evidence Lane toolchain" }: { children?: ReactNode; className?: string; label?: string }) {
  return <GlassShell className={`toolchain-shell ${className}`.trim()} label={label}>{children}</GlassShell>;
}
