import type { ButtonHTMLAttributes, CSSProperties, ReactNode } from "react";
import { t023UiSystemSchemas } from "../contracts/T023UniversalUiSystemSchemas";

export type GlassPillVariant = "default" | "active-cyan" | "active-gold" | "success" | "warning" | "danger" | "disabled";
export type UniversalPillState = "idle" | "active" | "success" | "warning" | "error";
export type UniversalPillTone = "neutral" | "cyan" | "gold";

export type GlassPillProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> & {
  children?: ReactNode;
  leading?: ReactNode;
  trailing?: ReactNode;
  iconOnly?: boolean;
  geometrySchema?: "universal" | "side-rail";
  variant?: GlassPillVariant;
  state?: UniversalPillState;
  tone?: UniversalPillTone;
  width?: number | string;
  minHeight?: number | string;
};

type PillStyle = CSSProperties & { "--pill-width"?: string; "--pill-height"?: string };
const cssSize = (value: number | string | undefined) => typeof value === "number" ? `${value}px` : value;

const legacyVariant = (state: UniversalPillState, tone: UniversalPillTone, disabled?: boolean): GlassPillVariant => {
  if (disabled) return "disabled";
  if (state === "success") return "success";
  if (state === "warning") return "warning";
  if (state === "error") return "danger";
  if (state === "active") return tone === "gold" ? "active-gold" : "active-cyan";
  return "default";
};

export function GlassPill({
  children,
  leading,
  trailing,
  iconOnly = false,
  geometrySchema = "universal",
  variant,
  state = "idle",
  tone = "neutral",
  width,
  minHeight,
  className = "",
  style,
  type = "button",
  disabled,
  ...props
}: GlassPillProps) {
  const resolvedVariant = variant ?? legacyVariant(state, tone, disabled);
  const pillStyle: PillStyle = { ...style, "--pill-width": cssSize(width), "--pill-height": cssSize(minHeight) };
  const iconOnlyPayload = leading ?? trailing;
  const resolvedIconOnlyPayload = iconOnlyPayload ?? children;
  return (
    <button
      {...props}
      type={type}
      disabled={disabled || resolvedVariant === "disabled"}
      className={`glass-pill glass-pill--${resolvedVariant} universal-pill universal-pill--${tone} universal-pill--${state} ${className}`.trim()}
      style={pillStyle}
      data-pill-variant={resolvedVariant}
      data-universal-pill-schema={t023UiSystemSchemas.universalGlassPill}
      data-pill-content-mode={iconOnly ? "icon-only" : "text"}
      data-pill-geometry-schema={geometrySchema}
      data-pill-has-leading={leading ? "true" : "false"}
      data-pill-has-trailing={trailing ? "true" : "false"}
      data-pill-containment="no-overlap"
      data-pill-edge-inset-policy="symmetric-content-box"
      data-pill-inline-layout={iconOnly ? "icon-only-center" : leading && trailing ? "leading-label-trailing" : leading ? "leading-label" : trailing ? "label-trailing" : "label-only"}
    >
      <span className="universal-pill__content glass-pill__content">
        {iconOnly && resolvedIconOnlyPayload && <span className="universal-pill__slot universal-pill__slot--icon-only-center glass-pill__slot glass-pill__slot--icon-only-center" data-pill-slot="icon-only-center">{resolvedIconOnlyPayload}</span>}
        {!iconOnly && leading && <span className="universal-pill__slot universal-pill__slot--leading glass-pill__slot glass-pill__slot--leading" data-pill-slot="leading">{leading}</span>}
        {!iconOnly && <span className="universal-pill__label glass-pill__label">{children}</span>}
        {!iconOnly && trailing && <span className="universal-pill__slot universal-pill__slot--trailing glass-pill__slot glass-pill__slot--trailing" data-pill-slot="trailing">{trailing}</span>}
      </span>
    </button>
  );
}
