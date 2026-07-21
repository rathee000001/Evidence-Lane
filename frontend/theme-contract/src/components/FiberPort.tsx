import type { CSSProperties } from "react";

type PortStyle = CSSProperties & { "--fiber-port-x": string; "--fiber-port-y": string };

export type FiberPortProps = {
  portId: string;
  x: number;
  y: number;
  tone: "cyan" | "gold";
  label: string;
};

export function FiberPort({ portId, x, y, tone, label }: FiberPortProps) {
  const style: PortStyle = { "--fiber-port-x": `${x * 100}%`, "--fiber-port-y": `${y * 100}%` };
  return <span className={`fiber-port fiber-port--${tone}`} style={style} data-port-id={portId} role="img" aria-label={label}><i /></span>;
}
