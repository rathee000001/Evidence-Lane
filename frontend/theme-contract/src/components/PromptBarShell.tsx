import type { ReactNode } from "react";
import { GlassShell } from "./GlassShell";

export function PromptBarShell({ children, className = "", label = "SQLite Builder command bar" }: { children?: ReactNode; className?: string; label?: string }) {
  return <GlassShell className={`prompt-bar-shell ${className}`.trim()} label={label} pillCluster="prompt-bar">{children}</GlassShell>;
}
