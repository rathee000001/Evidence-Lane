import { GlassIconOrb } from "./GlassIconOrb";

export function MetricIdentityOrb({ src, label, tone }: { src: string; label: string; tone: "gpu" | "cpu" | "ram" | "storage" }) {
  const color = { gpu: "#55d4f3", cpu: "#69d9f5", ram: "#82e6cf", storage: "#efc96f" }[tone];
  return (
    <GlassIconOrb className={`metric-identity-orb metric-identity-orb--${tone}`} color={color} size="clamp(46px,3.15vw,56px)" label={label}>
      <img src={src} alt="" />
    </GlassIconOrb>
  );
}
