import chatGPTIcon from "../assets/identity/chatgpt-logo.webp";
import codexIcon from "../assets/identity/codex-color.png";
import geminiIcon from "../assets/identity/gemini-icon.webp";
import leftCore from "../generated/logo/left_core.svg?raw";
import { GlassIconOrb } from "./GlassIconOrb";

export type CommandIdentity = "chatgpt" | "ollama" | "gemini" | "custom" | "telemetry" | "build" | "folder" | "codex";
const svgPaths = (svg: string) => svg.replace(/^<svg[^>]*>/, "").replace(/<\/svg>\s*$/, "");

function UtilityMark({ identity }: { identity: "ollama" | "custom" | "build" | "folder" }) {
  const mark = {
    ollama: <><path d="M10 8v12c0 5 3 8 6 8s6-3 6-8V8" /><path d="M10 11 7 7M22 11l3-4M13 17h6M14 22h4" /></>,
    custom: <><circle cx="9" cy="16" r="3" /><circle cx="23" cy="9" r="3" /><circle cx="23" cy="23" r="3" /><path d="m12 15 8-5M12 17l8 5" /></>,
    build: <><path d="m9 5 14 11-14 11Z" /><path d="M22 5h5v22h-5" /></>,
    folder: <><path d="M4 9h10l3 3h11v14H4Z" /><path d="M7 16h18" /></>,
  }[identity];
  return <svg viewBox="0 0 32 32" aria-hidden="true">{mark}</svg>;
}

export function CommandIdentityOrb({ identity, className = "" }: { identity: CommandIdentity; className?: string }) {
  const color = {
    chatgpt: "#69d9f5",
    ollama: "#718392",
    gemini: "#a8a0ee",
    custom: "#efca72",
    telemetry: "#70d8ef",
    build: "#77d8ad",
    folder: "#efca72",
    codex: "#7dc9ed",
  }[identity];

  return <GlassIconOrb className={`command-identity-orb command-identity-orb--${identity} ${className}`.trim()} color={color} decorative>
    {identity === "chatgpt" ? <img src={chatGPTIcon} alt="" /> : identity === "gemini" ? <img src={geminiIcon} alt="" /> : identity === "codex" ? <img src={codexIcon} alt="" /> : identity === "telemetry" ? <svg viewBox="40 510 800 800" preserveAspectRatio="xMidYMid meet"><g dangerouslySetInnerHTML={{ __html: svgPaths(leftCore) }} /></svg> : <UtilityMark identity={identity} />}
  </GlassIconOrb>;
}
