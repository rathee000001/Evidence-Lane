import { useId } from "react";

export type CubeAppIconProps = {
  size?: number | string;
  animated?: boolean;
  className?: string;
};

export function CubeAppIcon({ size = 128, animated = true, className = "" }: CubeAppIconProps) {
  const id = useId().replace(/:/g, "");

  return (
    <svg
      className={`evidence-cube ${animated ? "is-animated" : ""} ${className}`.trim()}
      width={size}
      height={size}
      viewBox="0 0 256 256"
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label="Evidence Lane cube"
    >
      <defs>
        <linearGradient id={`${id}-top`} x1="20%" y1="8%" x2="82%" y2="88%">
          <stop offset="0" stopColor="#f7f9ff" stopOpacity=".92" />
          <stop offset=".46" stopColor="#d8deed" stopOpacity=".82" />
          <stop offset="1" stopColor="#ffe6d4" stopOpacity=".88" />
        </linearGradient>
        <linearGradient id={`${id}-left`} x1="4%" y1="15%" x2="96%" y2="92%">
          <stop offset="0" stopColor="#62aef7" stopOpacity=".88" />
          <stop offset=".58" stopColor="#9dbce8" stopOpacity=".76" />
          <stop offset="1" stopColor="#c8d3ed" stopOpacity=".88" />
        </linearGradient>
        <linearGradient id={`${id}-right`} x1="8%" y1="8%" x2="92%" y2="94%">
          <stop offset="0" stopColor="#ffe8d7" stopOpacity=".93" />
          <stop offset=".52" stopColor="#e7e1e6" stopOpacity=".82" />
          <stop offset="1" stopColor="#cdd8ed" stopOpacity=".9" />
        </linearGradient>
        <linearGradient id={`${id}-shine`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="white" stopOpacity=".75" />
          <stop offset=".45" stopColor="white" stopOpacity="0" />
          <stop offset="1" stopColor="#8be9ff" stopOpacity=".28" />
        </linearGradient>
        <filter id={`${id}-shadow`} x="-40%" y="-40%" width="180%" height="200%">
          <feDropShadow dx="0" dy="15" stdDeviation="13" floodColor="#25395d" floodOpacity=".3" />
          <feDropShadow dx="-4" dy="0" stdDeviation="6" floodColor="#4fdfff" floodOpacity=".16" />
        </filter>
      </defs>
      <g filter={`url(#${id}-shadow)`}>
        <path className="evidence-cube__top" d="M128 20 224 76 128 132 32 76Z" fill={`url(#${id}-top)`} />
        <path className="evidence-cube__left" d="M32 76 128 132v104l-96-56Z" fill={`url(#${id}-left)`} />
        <path className="evidence-cube__right" d="m128 132 96-56v104l-96 56Z" fill={`url(#${id}-right)`} />
        <path d="M128 20 224 76 128 132 32 76Z" fill={`url(#${id}-shine)`} opacity=".54" />
        <path d="M32 76 128 132v104l-96-56Z" fill="none" stroke="#d9f7ff" strokeOpacity=".62" strokeWidth="1.4" />
        <path d="m128 132 96-56v104l-96 56Z" fill="none" stroke="#fff7ee" strokeOpacity=".72" strokeWidth="1.4" />
        <path d="M128 20 224 76 128 132 32 76Z" fill="none" stroke="white" strokeOpacity=".72" strokeWidth="1.4" />
        <g className="evidence-cube__mark" fill="#f4f7ff" fillOpacity=".68">
          <path d="m57 119 51 30v17l-51-30Z" />
          <path d="m59 157 49 29v17l-49-29Z" />
          <path d="m148 145 55-32v17l-55 32Z" />
          <path d="m148 184 55-32v17l-55 32Z" />
        </g>
        <path className="evidence-cube__scan" d="M34 77 128 132 222 77" fill="none" stroke="white" strokeOpacity=".42" strokeWidth="5" />
      </g>
    </svg>
  );
}
