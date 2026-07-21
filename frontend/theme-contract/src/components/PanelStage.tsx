import type { ReactNode } from "react";

export type PanelStageProps = {
  children?: ReactNode;
  className?: string;
  label: string;
};

export function PanelStage({ children, className = "", label }: PanelStageProps) {
  return (
    <section className={`panel-stage ${className}`.trim()} aria-label={label} data-t023-stage>
      <div className="panel-stage__content">{children}</div>
    </section>
  );
}
