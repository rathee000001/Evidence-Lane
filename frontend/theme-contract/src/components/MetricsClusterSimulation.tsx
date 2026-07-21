import { useEffect, useState } from "react";
import { MetricsClusterPanel } from "./MetricsClusterPanel";

const wave = (tick: number, phase: number, speed = 1) => Math.round(50 + 50 * Math.sin((tick * speed + phase) * Math.PI / 18));

export function MetricsClusterSimulation() {
  const [tick, setTick] = useState(2);
  useEffect(() => { const timer = window.setInterval(() => setTick((value) => value + 1), 520); return () => window.clearInterval(timer); }, []);
  return (
    <main className="metrics-demo">
      <MetricsClusterPanel gpu={wave(tick, 5, .83)} cpu={wave(tick, 0, 1)} ram={wave(tick, 10, .72)} read={wave(tick, 14, .91)} write={wave(tick, 2, .63)} demoActive />
      <span className="metrics-demo__connection">UI simulation · DEMO values · components accept external props</span>
    </main>
  );
}
