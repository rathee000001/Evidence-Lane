import { useId, type CSSProperties } from "react";
import { metricState, metricStateClass, normalizeMetricValue } from "./metricState";

export type MetricGaugeProps = { value: number | null; label: string };

const ripplePaths = [
  "M18 70C8 90 8 120 18 140", "M8 62C-5 87-5 123 8 148", "M-2 55C-18 84-18 126-2 155",
  "M202 70C212 90 212 120 202 140", "M212 62C225 87 225 123 212 148", "M222 55C238 84 238 126 222 155",
];

export function MetricGauge({ value, label }: MetricGaugeProps) {
  const { available, safe } = normalizeMetricValue(value);
  const state = metricState(value);
  const angle = -130 + safe * 2.6;
  const gradientId = useId().replace(/:/g, "");
  const style = { "--metric-value": safe } as CSSProperties;
  return (
    <div className={`metric-gauge ${metricStateClass(state)}`} data-metric-state={state} style={style} aria-label={`${label} ${available ? `${safe}%` : "idle"}`}>
      <svg className="metric-gauge__dial" viewBox="0 0 220 175" fill="none" aria-hidden="true">
        <defs>
          <linearGradient id={`${gradientId}-fiber`} x1="50" y1="155" x2="174" y2="54" gradientUnits="userSpaceOnUse">
            <stop offset="0" stopColor="var(--metric-accent)" stopOpacity=".92" />
            <stop offset=".5" stopColor="var(--metric-accent)" />
            <stop offset="1" stopColor="var(--metric-accent)" stopOpacity=".9" />
          </linearGradient>
        </defs>
        <g className="metric-gauge__ripples metric-gauge__ripples--sheath" fill="none">{ripplePaths.map((path, index) => <path fill="none" d={path} key={index} />)}</g>
        <g className="metric-gauge__ripples metric-gauge__ripples--core" fill="none">{ripplePaths.map((path, index) => <path fill="none" d={path} key={index} />)}</g>
        <path className="metric-gauge__tube-shell" pathLength="100" d="M53 160A74 74 0 1 1 167 160" />
        <path className="metric-gauge__tube-channel" pathLength="100" d="M53 160A74 74 0 1 1 167 160" />
        <path className="metric-gauge__fiber-glow" pathLength="100" strokeDasharray={`${safe} 100`} d="M53 160A74 74 0 1 1 167 160" />
        <path className="metric-gauge__fiber-body" stroke={`url(#${gradientId}-fiber)`} pathLength="100" strokeDasharray={`${safe} 100`} d="M53 160A74 74 0 1 1 167 160" />
        <path className="metric-gauge__fiber-highlight" pathLength="100" strokeDasharray={`${safe} 100`} d="M53 160A74 74 0 1 1 167 160" />
        <g className="metric-gauge__dots" fill="currentColor">
          {Array.from({ length: 7 }, (_, index) => {
            const dotAngle = (140 + index * (260 / 6)) * Math.PI / 180;
            return <circle fill="currentColor" key={index} cx={110 + Math.cos(dotAngle) * 57} cy={112 + Math.sin(dotAngle) * 57} r="2.1" />;
          })}
        </g>
        <g className="metric-gauge__needle" style={{ transform: `rotate(${angle}deg)` }}><path d="M106 112 110 38l4 74Z" /></g>
        <circle className="metric-gauge__hub-outer" cx="110" cy="112" r="16" />
        <circle className="metric-gauge__hub" cx="110" cy="112" r="9" />
      </svg>
      <strong className="metric-gauge__value">{available ? `${Math.round(safe)}%` : "Idle"}</strong>
    </div>
  );
}
