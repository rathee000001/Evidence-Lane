import { useState, type ReactNode } from "react";
import { GlassPill } from "./GlassPill";

export function FieldStack({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`field-stack ${className}`.trim()}>{children}</div>;
}

export function ReadableIconSlot({ children, label, className = "" }: { children: ReactNode; label: string; className?: string }) {
  return <span className={`readable-icon-slot ${className}`.trim()} role="img" aria-label={label}>{children}</span>;
}

export type MetricGlyphType = "gpu" | "cpu" | "ram" | "storage";

export function MetricGlyph({ type }: { type: MetricGlyphType }) {
  const glyph = {
    gpu: <><rect x="5" y="6" width="14" height="12" rx="2" /><path d="M8 10h8M8 14h5M9 3v3M15 3v3M9 18v3M15 18v3" /></>,
    cpu: <><rect x="6" y="6" width="12" height="12" rx="2" /><rect x="9" y="9" width="6" height="6" rx="1" /><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3" /></>,
    ram: <><rect x="3" y="8" width="18" height="8" rx="2" /><path d="M7 11v2M11 11v2M15 11v2M5 16v3M9 16v3M15 16v3M19 16v3" /></>,
    storage: <><ellipse cx="12" cy="6" rx="8" ry="3" /><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></>,
  }[type];
  return <svg viewBox="0 0 24 24" aria-hidden="true">{glyph}</svg>;
}

export function MetricField({ label, value, meta, className = "" }: { label: string; value: ReactNode; meta?: ReactNode; className?: string }) {
  return (
    <div className={`metric-field ${className}`.trim()}>
      <span className="metric-field__label">{label}</span>
      <strong className="metric-field__value">{value}</strong>
      {meta && <span className="metric-field__meta">{meta}</span>}
    </div>
  );
}

export function StatusField({ label, title, detail, tone = "cyan" }: { label: string; title: string; detail?: string; tone?: "cyan" | "gold" | "green" | "red" }) {
  return (
    <div className={`status-field status-field--${tone}`}>
      <span className="status-field__dot" aria-hidden="true" />
      <div>
        <span className="status-field__label">{label}</span>
        <strong className="status-field__title">{title}</strong>
        {detail && <span className="status-field__detail">{detail}</span>}
      </div>
    </div>
  );
}

export function ExpandableDetailSlot({ label = "Details", children }: { label?: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`expandable-detail-slot ${open ? "is-open" : ""}`}>
      <GlassPill variant={open ? "active-gold" : "default"} onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        {label}
      </GlassPill>
      {open && <div className="expandable-detail-slot__panel">{children}</div>}
    </div>
  );
}
