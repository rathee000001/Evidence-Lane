export type SourceLaneIconProps = { lane: string; size?: number | string; className?: string };

function StrokeIcon({ children, lane, size, className = "" }: SourceLaneIconProps & { children: React.ReactNode }) {
  return <svg className={`source-lane-icon ${className}`.trim()} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" role="img" aria-label={`${lane.replaceAll("_", " ")} source lane`}>{children}</svg>;
}

export function SourceLaneIcon({ lane, size = 22, className = "" }: SourceLaneIconProps) {
  if (lane === "github_code") return <svg className={`source-lane-icon ${className}`.trim()} width={size} height={size} viewBox="0 0 24 24" fill="currentColor" role="img" aria-label="GitHub source lane"><path d="M12 2.35a9.85 9.85 0 0 0-3.11 19.2c.49.09.67-.21.67-.47v-1.72c-2.73.59-3.3-1.16-3.3-1.16-.45-1.14-1.09-1.45-1.09-1.45-.89-.61.07-.6.07-.6.98.07 1.5 1.01 1.5 1.01.88 1.5 2.3 1.07 2.86.82.09-.63.34-1.07.62-1.32-2.18-.25-4.47-1.09-4.47-4.86 0-1.07.38-1.95 1.01-2.64-.1-.25-.44-1.25.1-2.61 0 0 .82-.26 2.7 1.01A9.35 9.35 0 0 1 12 7.24c.84 0 1.66.11 2.45.33 1.87-1.27 2.7-1.01 2.7-1.01.53 1.36.2 2.36.1 2.61.63.69 1.01 1.57 1.01 2.64 0 3.78-2.3 4.61-4.48 4.85.35.3.66.9.66 1.81v2.61c0 .26.18.56.67.47A9.85 9.85 0 0 0 12 2.35Z" /></svg>;
  const props = { lane, size, className };
  if (lane === "local_code") return <StrokeIcon {...props}><path d="M3 7.5h7l2 2h9v9.5H3Z" /><path d="m9 13-2 2 2 2m6-4 2 2-2 2m-3-5-1 6" /></StrokeIcon>;
  if (lane === "chat_lineage") return <StrokeIcon {...props}><path d="M4 5h12v9H9l-4 3v-3H4Z" /><path d="M9 8h11v9h-3v3l-4-3h-2" /><path d="M7 9h6M7 11.5h4" /></StrokeIcon>;
  if (lane === "discussion") return <StrokeIcon {...props}><path d="M3 5h18v11H9l-5 4v-4H3Z" /><circle cx="8" cy="10.5" r=".8" fill="currentColor" /><circle cx="12" cy="10.5" r=".8" fill="currentColor" /><circle cx="16" cy="10.5" r=".8" fill="currentColor" /></StrokeIcon>;
  if (lane === "analysis") return <StrokeIcon {...props}><path d="M4 19V9m5 10V5m5 14v-7" /><circle cx="17.5" cy="7.5" r="3.5" /><path d="m20 10 2 2" /></StrokeIcon>;
  if (lane === "plan") return <StrokeIcon {...props}><rect x="5" y="4" width="14" height="17" rx="2" /><path d="M9 4V2h6v2M8 9h8M8 13h4m3 0 1 1 2-3M8 17h8" /></StrokeIcon>;
  if (lane === "mode") return <StrokeIcon {...props}><path d="M12 2.5 20 6v5c0 5.2-3.4 8.7-8 10.5C7.4 19.7 4 16.2 4 11V6Z" /><path d="M8 10h8M8 14h8" /><circle cx="10" cy="10" r="1.2" fill="currentColor" /><circle cx="14" cy="14" r="1.2" fill="currentColor" /></StrokeIcon>;
  if (lane === "documents") return <StrokeIcon {...props}><path d="M6 2.5h8l4 4V22H6Z" /><path d="M14 2.5V7h4M8 11l1.4 6 1.6-4 1.6 4 1.4-6" /></StrokeIcon>;
  if (lane === "data_excel") return <StrokeIcon {...props}><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M8 4v16m4-16v16m4-16v16M3 9h18M3 14h18" /><path d="m5 16 2 2m0-2-2 2" /></StrokeIcon>;
  if (lane === "powerpoint") return <StrokeIcon {...props}><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M8 17V7h3.2a3 3 0 0 1 0 6H8m8-5h2m-2 4h2m-2 4h2" /></StrokeIcon>;
  if (lane === "pdf_ocr") return <StrokeIcon {...props}><path d="M6 2.5h8l4 4V22H6Z" /><path d="M14 2.5V7h4M8 16v-5h2a1.5 1.5 0 0 1 0 3H8m5-3v5h1.2c1.8 0 2.8-.9 2.8-2.5S16 11 14.2 11Z" /></StrokeIcon>;
  if (lane === "images_ocr") return <StrokeIcon {...props}><rect x="4" y="5" width="16" height="14" rx="2" /><circle cx="9" cy="10" r="1.5" /><path d="m6 17 4-4 3 3 2-2 3 3M2 8V3h5m15 5V3h-5M2 16v5h5m15-5v5h-5" /></StrokeIcon>;
  if (lane === "artifacts") return <StrokeIcon {...props}><path d="m4 7 8-4 8 4-8 4Z" /><path d="M4 7v10l8 4 8-4V7M12 11v10" /></StrokeIcon>;
  if (lane === "custom") return <StrokeIcon {...props}><path d="M9 4H7a2 2 0 0 0-2 2v3a2 2 0 0 1-2 2 2 2 0 0 1 2 2v3a2 2 0 0 0 2 2h2m6-14h2a2 2 0 0 1 2 2v3a2 2 0 0 0 2 2 2 2 0 0 0-2 2v3a2 2 0 0 1-2 2h-2" /><path d="m10 15 4-6" /></StrokeIcon>;
  if (lane === "brain_loader") return <StrokeIcon {...props}><path d="M8 18a4 4 0 0 1-3-6.6A4.5 4.5 0 0 1 9.5 5 4 4 0 0 1 17 6.8a4 4 0 0 1 1 7.7A4 4 0 0 1 14 19H8" /><path d="M9 8v8m6-8v8m-3-4v8m-2-2 2 2 2-2" /></StrokeIcon>;
  if (lane === "research") return <StrokeIcon {...props}><path d="M9 3h6m-4 0v5l-5 9a2.5 2.5 0 0 0 2.2 3.5h7.6A2.5 2.5 0 0 0 18 17l-5-9V3" /><path d="M8 15h8" /><circle cx="16.5" cy="8.5" r="2.5" /><path d="m18.2 10.2 2 2" /></StrokeIcon>;
  if (lane === "project_engulf") return <StrokeIcon {...props}><path d="M3 7h7l2 2h9v10H3Z" /><path d="M16 3v6m-2-2 2 2 2-2M7 14h7m-2-2 2 2-2 2" /></StrokeIcon>;
  return <StrokeIcon {...props}><ellipse cx="12" cy="5" rx="7" ry="3" /><path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" /><path d="M10 8.5h4m-2-2v4" /></StrokeIcon>;
}
