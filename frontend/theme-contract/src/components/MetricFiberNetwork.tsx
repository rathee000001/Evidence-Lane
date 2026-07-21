import type { CSSProperties } from "react";

const colors = ["#72e6ff", "#8bbcff", "#e9fbff", "#78efc1", "#f4d58d"];
const ramToCpu = [
  "M276 372V368Q276 356 288 356H476Q488 356 488 348",
  "M280 372V369Q280 359 290 359H479Q491 359 491 349",
  "M284 372V370Q284 362 292 362H482Q494 362 494 350",
  "M288 372V371Q288 365 294 365H485Q497 365 497 351",
  "M292 372V372Q292 368 296 368H488Q500 368 500 352",
];
const ssdToCpu = [
  "M630 372V369Q630 357 642 357H674Q686 357 686 349",
  "M634 372V370Q634 360 644 360H677Q689 360 689 350",
  "M638 372V371Q638 363 646 363H680Q692 363 692 351",
  "M642 372V372Q642 366 648 366H683Q695 366 695 352",
  "M646 372V372Q646 369 650 369H686Q698 369 698 353",
];

export function MetricFiberNetwork() {
  return (
    <svg className="metric-fiber-network" viewBox="0 0 920 652" preserveAspectRatio="none" fill="none" aria-hidden="true">
      {[ramToCpu, ssdToCpu].map((bundle, bundleIndex) => (
        <g className="metric-fiber-bundle" key={bundleIndex}>
          {bundle.map((path, index) => (
            <g key={index} style={{ "--fiber-color": colors[index], "--fiber-delay": `${index * -.42}s` } as CSSProperties}>
              <path fill="none" className="metric-fiber-strand__sheath" d={path} />
              <path fill="none" className="metric-fiber-strand__body" d={path} />
              <path fill="none" className="metric-fiber-strand__highlight" d={path} />
            </g>
          ))}
        </g>
      ))}
    </svg>
  );
}
