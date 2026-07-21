import manifest from "./T023UniversalThemeManifest.json";

export const THEME_ICON_GEOMETRY_OWNER = "GlassIconOrb" as const;
export const MAX_THEME_SVG_BYTES = 262_144;

export type ThemeIconSlotId = keyof typeof manifest.icon_asset_slots;

export type ThemeSvgValidation =
  | { ok: true; normalizedSvg: string }
  | { ok: false; code: "INVALID_SVG" | "UNSAFE_SVG" | "SVG_TOO_LARGE"; reason: string };

export type ThemeIconAssetResolution =
  | {
      ok: true;
      slot: ThemeIconSlotId;
      kind: "built-in" | "validated-local-svg-candidate";
      asset: string;
      fallbackAsset: string;
      geometryOwner: typeof THEME_ICON_GEOMETRY_OWNER;
      geometryProfileId: string;
      validatedSvgCandidate?: string;
    }
  | { ok: false; code: "UNKNOWN_SLOT" | "INVALID_SVG" | "UNSAFE_SVG" | "SVG_TOO_LARGE"; reason: string };

const UNSAFE_SVG_PATTERNS: ReadonlyArray<{ reason: string; pattern: RegExp }> = [
  { reason: "script elements are forbidden", pattern: /<script\b/i },
  { reason: "foreignObject elements are forbidden", pattern: /<foreignObject\b/i },
  { reason: "embedded browsing or media is forbidden", pattern: /<(?:iframe|object|embed|audio|video)\b/i },
  { reason: "animation elements are forbidden", pattern: /<(?:animate|animateMotion|animateTransform|set|discard)\b/i },
  { reason: "inline event handlers are forbidden", pattern: /on\w+\s*=/i },
  { reason: "javascript URLs are forbidden", pattern: /javascript:/i },
  { reason: "remote URLs are forbidden", pattern: /https?:\/\//i },
  { reason: "CSS imports are forbidden", pattern: /@import/i },
  { reason: "document types and entities are forbidden", pattern: /<!DOCTYPE|<!ENTITY/i },
  { reason: "style elements are forbidden", pattern: /<style\b/i },
  { reason: "data URLs are forbidden", pattern: /data\s*:/i },
  { reason: "external href references are forbidden", pattern: /(?:href|xlink:href)\s*=\s*["'](?!#)/i },
  { reason: "external CSS resources are forbidden", pattern: /url\s*\(/i },
];

// Reject https?:// assets; a future Theme Page may copy only a validated local SVG.
export function validateThemeSvg(svgText: string): ThemeSvgValidation {
  const normalizedSvg = svgText.trim();
  if (!normalizedSvg || !/^<svg\b[^>]*>/i.test(normalizedSvg) || !/<\/svg>$/i.test(normalizedSvg)) {
    return { ok: false, code: "INVALID_SVG", reason: "A complete root svg element is required." };
  }
  if (new TextEncoder().encode(normalizedSvg).byteLength > MAX_THEME_SVG_BYTES) {
    return { ok: false, code: "SVG_TOO_LARGE", reason: `SVG exceeds ${MAX_THEME_SVG_BYTES} bytes.` };
  }
  for (const rule of UNSAFE_SVG_PATTERNS) {
    if (rule.pattern.test(normalizedSvg)) {
      return { ok: false, code: "UNSAFE_SVG", reason: rule.reason };
    }
  }
  return { ok: true, normalizedSvg };
}

function isThemeIconSlot(slot: string): slot is ThemeIconSlotId {
  return Object.prototype.hasOwnProperty.call(manifest.icon_asset_slots, slot);
}

export function resolveThemeIconAsset(slot: string, localSvgCandidate?: string): ThemeIconAssetResolution {
  if (!isThemeIconSlot(slot)) {
    return { ok: false, code: "UNKNOWN_SLOT", reason: `Unknown theme icon slot: ${slot}` };
  }

  const contract = manifest.icon_asset_slots[slot];
  const geometry = {
    geometryOwner: THEME_ICON_GEOMETRY_OWNER,
    geometryProfileId: manifest.geometry_profile_id,
  } as const;

  if (localSvgCandidate === undefined) {
    return {
      ok: true,
      slot,
      kind: "built-in",
      asset: contract.built_in_asset,
      fallbackAsset: contract.fallback_asset,
      ...geometry,
    };
  }

  const validation = validateThemeSvg(localSvgCandidate);
  if (!validation.ok) return validation;

  return {
    ok: true,
    slot,
    kind: "validated-local-svg-candidate",
    asset: `managed:${manifest.asset_policy.managed_directory}/${slot}.svg`,
    fallbackAsset: contract.fallback_asset,
    validatedSvgCandidate: validation.normalizedSvg,
    ...geometry,
  };
}
