import type { CSSProperties } from "react";
import manifest from "./T023UniversalThemeManifest.json";

export type EvidenceThemeDefinition = {
  id: string;
  manifestVersion: "T023_THEME_MANIFEST_V001";
  themeVersion: string;
  geometryProfileId: string;
  module: string;
  className: string;
  surfaces: readonly string[];
  tokens: Record<`--${string}`, string>;
  manifestHash: string;
  manifest: typeof manifest;
};

export type EvidenceThemeStyle = CSSProperties & Record<`--${string}`, string>;

export const universalThemeManifest = Object.freeze(manifest);

export const sqliteGlassTheme: EvidenceThemeDefinition = Object.freeze({
  id: manifest.theme_id,
  manifestVersion: "T023_THEME_MANIFEST_V001",
  themeVersion: manifest.theme_version,
  geometryProfileId: manifest.geometry_profile_id,
  module: manifest.module,
  className: manifest.class_name,
  surfaces: ["default", "transparent", "frosted-popup"],
  tokens: manifest.runtime_css_tokens,
  manifestHash: manifest.manifest_hash,
  manifest,
});

export const sqliteGlassThemeStyle = sqliteGlassTheme.tokens as EvidenceThemeStyle;
