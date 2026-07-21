import type { CSSProperties, ReactNode } from "react";
import { t023UiSystemSchemas } from "../contracts/T023UniversalUiSystemSchemas";
import { useUniversalPillCluster } from "./UniversalPillCluster";

export type GlassSurface = "default" | "transparent" | "frosted-popup";

export type GlassShellProps = {
  children?: ReactNode;
  className?: string;
  width?: number | string;
  height?: number | string;
  radius?: number | string;
  as?: "section" | "footer" | "header" | "aside" | "main" | "div" | "nav";
  label?: string;
  surface?: GlassSurface;
  pillCluster?: string;
  "data-metric-state"?: string;
  "data-shared-rear-glass"?: string;
};

type ShellStyle = CSSProperties & { "--shell-radius"?: string };
const unit = (value: number | string | undefined) => typeof value === "number" ? `${value}px` : value;

const surfaceClass: Record<GlassSurface, string> = {
  default: "",
  transparent: "glass-shell--transparent",
  "frosted-popup": "glass-shell--frosted-popup",
};

export function GlassShell({ children, className = "", width, height, radius, as: Tag = "section", label, surface = "default", pillCluster, ...dataAttributes }: GlassShellProps) {
  const style: ShellStyle = { width, height, "--shell-radius": unit(radius) };
  const pillClusterRef = useUniversalPillCluster(pillCluster);
  return (
    <Tag
      {...dataAttributes}
      className={`glass-shell ${surfaceClass[surface]} ${className}`.trim()}
      style={style}
      aria-label={label}
      data-glass-surface={surface}
      data-universal-glass-schema={t023UiSystemSchemas.universalGlassShell}
      data-glass-layering-schema={t023UiSystemSchemas.glassLayeringNoBleed}
      data-popup-schema={surface === "frosted-popup" ? t023UiSystemSchemas.universalFrostedPopup : undefined}
      data-popup-motion-schema={surface === "frosted-popup" ? t023UiSystemSchemas.universalPopupFade : undefined}
    >
      <div ref={pillClusterRef} className="glass-shell__content" data-universal-pill-cluster={pillCluster} data-universal-prompt-bar={pillCluster === "prompt-bar" ? "distributed" : undefined} data-universal-prompt-distribution-law={pillCluster === "prompt-bar" ? "T023_SIX_EQUAL_COMMAND_PILLS_V1" : undefined}>{children}</div>
    </Tag>
  );
}
