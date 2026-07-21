import type { CSSProperties, ReactNode } from "react";
import { t023UiSystemSchemas } from "../contracts/T023UniversalUiSystemSchemas";

type GlassIconOrbStyle = CSSProperties & {
  "--glass-orb-color"?: string;
  "--glass-orb-size"?: string;
};

export type GlassIconOrbProps = {
  children: ReactNode;
  className?: string;
  color?: string;
  decorative?: boolean;
  label?: string;
  size?: number | string;
};

/**
 * The single non-brain identity-orb surface used throughout the SQLite theme.
 * Material layers use pseudo-elements so callers cannot stack glass wrappers.
 */
export function GlassIconOrb({
  children,
  className = "",
  color = "#69d9f5",
  decorative = false,
  label,
  size = 36,
}: GlassIconOrbProps) {
  const resolvedSize = typeof size === "number" ? `${size}px` : size;
  const style: GlassIconOrbStyle = {
    "--glass-orb-color": color,
    "--glass-orb-size": resolvedSize,
  };

  return (
    <span
      className={`glass-icon-orb ${className}`.trim()}
      style={style}
      data-glass-orb-host="true"
      data-universal-orb-schema={t023UiSystemSchemas.universalGlassOrb}
      data-glass-orb-content-origin="0,0"
      role={!decorative && label ? "img" : undefined}
      aria-label={!decorative ? label : undefined}
      aria-hidden={decorative || undefined}
    >
      <span className="glass-icon-orb__content"><span className="glass-icon-orb__payload">{children}</span></span>
    </span>
  );
}
