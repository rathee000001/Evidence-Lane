import { useState } from "react";
import { BrainGlassSphere } from "./BrainGlassSphere";
import { CommandIdentityOrb } from "./CommandIdentityOrb";
import { GlassIconOrb } from "./GlassIconOrb";
import { PromptBarShell } from "./PromptBarShell";
import { UniversalGlassPill } from "./UniversalGlassPill";

export type CommandKey = "source" | "chatgpt" | "ollama" | "gemini" | "brain-output" | "codex" | "build";
export type SQLiteCommandBarProps = { onCommand?: (command: CommandKey) => void; building?: boolean; complete?: boolean; openExternal?: boolean; activeBrainColor?: string };

export function SQLiteCommandBar({ onCommand, building = false, complete = false, openExternal = true, activeBrainColor = "#69d9f5" }: SQLiteCommandBarProps) {
  const [success, setSuccess] = useState<CommandKey | null>(null);
  const run = (command: CommandKey) => {
    const nativeShell = Boolean(window.__EVIDENCE_OS_NATIVE_COMMAND_TRANSPORT__);
    if (openExternal && !nativeShell && command === "chatgpt") window.open("https://chatgpt.com/", "_blank", "noopener,noreferrer");
    if (openExternal && !nativeShell && command === "gemini") window.open("https://gemini.google.com/app", "_blank", "noopener,noreferrer");
    onCommand?.(command);
    setSuccess(command);
    window.setTimeout(() => setSuccess((current) => current === command ? null : current), 1500);
  };
  const state = (key: CommandKey) => success === key || (key === "build" && complete) ? "success" : key === "build" && building ? "active" : "idle";
  return <PromptBarShell className="sqlite-command-bar" label="SQLite Builder command bar">
    <UniversalGlassPill className="sqlite-command-bar__source" data-source-intake-control="orb-only" data-pill-cluster-exempt="true" iconOnly leading={<GlassIconOrb color="#69d9f5" size={36} decorative><span className="sqlite-command-bar__plus-glyph">+</span></GlassIconOrb>} state={state("source")} onClick={() => run("source")} aria-label="Open source intake" />
    <UniversalGlassPill aria-label="ChatGPT" leading={<CommandIdentityOrb identity="chatgpt" />} state={state("chatgpt")} onClick={() => run("chatgpt")}>ChatGPT</UniversalGlassPill>
    <UniversalGlassPill aria-label="Ollama" className="sqlite-command-bar__ollama" leading={<CommandIdentityOrb identity="ollama" />} state={state("ollama")} onClick={() => run("ollama")} title="Launch the governed Ollama application">Ollama</UniversalGlassPill>
    <UniversalGlassPill aria-label="Gemini" leading={<CommandIdentityOrb identity="gemini" />} state={state("gemini")} onClick={() => run("gemini")}>Gemini</UniversalGlassPill>
    <UniversalGlassPill aria-label="Brain Output" leading={<BrainGlassSphere className="command-brain-orb" color={activeBrainColor} size={36} />} state={state("brain-output")} onClick={() => run("brain-output")}>Brain Output</UniversalGlassPill>
    <UniversalGlassPill aria-label="Codex" leading={<CommandIdentityOrb identity="codex" />} state={state("codex")} onClick={() => run("codex")} title="Launch the Codex desktop application">Codex</UniversalGlassPill>
    <UniversalGlassPill aria-label={building ? "Building" : "Build Command"} leading={<BrainGlassSphere className="command-brain-orb" color={activeBrainColor} size={36} />} state={state("build")} disabled={building} onClick={() => run("build")}>{building ? "Building" : "Build Command"}</UniversalGlassPill>
  </PromptBarShell>;
}
