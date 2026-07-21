import type { CSSProperties } from "react";

export type GlassFiberRoute = {
  routeId: string;
  sourceCard: string;
  sourcePort: string;
  targetCard: string;
  targetPort: string;
  tone: "cyan" | "gold";
  color: string;
  visualMeaning: string;
  paths: string[];
};

export const glassFiberRoutes: GlassFiberRoute[] = [
  {
    routeId: "fiber-ram-to-cpu",
    sourceCard: "RAM",
    sourcePort: "ram-out",
    targetCard: "CPU",
    targetPort: "cpu-in-left",
    tone: "cyan",
    color: "#66ddff",
    visualMeaning: "Memory pressure is routed to the processor field.",
    paths: [
      "M276 337V333Q276 321 288 321H476Q488 321 488 315",
      "M281 337V334Q281 325 290 325H480Q492 325 492 315",
      "M286 337V335Q286 329 294 329H484Q496 329 496 315"
    ]
  },
  {
    routeId: "fiber-storage-to-cpu",
    sourceCard: "SSD/HDD",
    sourcePort: "storage-out",
    targetCard: "CPU",
    targetPort: "cpu-in-right",
    tone: "gold",
    color: "#f2c96f",
    visualMeaning: "Storage activity is routed to the processor field.",
    paths: [
      "M630 337V334Q630 322 642 322H674Q686 322 686 315",
      "M635 337V335Q635 326 644 326H678Q690 326 690 315",
      "M640 337V336Q640 330 646 330H682Q694 330 694 315"
    ]
  }
];

export function GlassFiberNetwork({ demoActive = false }: { demoActive?: boolean }) {
  return (
    <svg className={`glass-fiber-network ${demoActive ? "is-demo-active" : ""}`} viewBox="0 0 920 652" preserveAspectRatio="none" fill="none" aria-hidden="true">
      <defs>
        <filter id="fiber-halo" x="-30%" y="-200%" width="160%" height="500%"><feGaussianBlur stdDeviation="5" /></filter>
      </defs>
      {glassFiberRoutes.map((route) => (
        <g className={`glass-fiber-route glass-fiber-route--${route.tone}`} data-route-id={route.routeId} key={route.routeId} style={{ "--fiber-route-color": route.color } as CSSProperties}>
          {route.paths.map((path, index) => <g className="glass-fiber-strand" key={path} style={{ "--fiber-strand-index": index } as CSSProperties}>
            <path className="glass-fiber-strand__halo" d={path} filter="url(#fiber-halo)" />
            <path className="glass-fiber-strand__tube" d={path} />
            <path className="glass-fiber-strand__core" d={path} />
            <path className="glass-fiber-strand__pulse" d={path} pathLength="100" />
          </g>)}
        </g>
      ))}
    </svg>
  );
}
