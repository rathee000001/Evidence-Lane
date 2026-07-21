import { GlassShell } from "./GlassShell";
import { PanelStage } from "./PanelStage";

export function ToolchainPlaceholder() {
  return (
    <GlassShell className="t023-toolchain-panel" label="Toolchain placeholder panel">
      <PanelStage className="toolchain-placeholder-stage" label="Future scanner placeholder stage">
        <header className="toolchain-placeholder__header">
          <div><span>FUTURE WORKSPACE</span><h2>Toolchain</h2></div>
          <strong>PLACEHOLDER</strong>
        </header>
        <div className="toolchain-placeholder__field">
          <span className="toolchain-placeholder__guide" aria-hidden="true" />
          <strong>FUTURE SCANNER STAGE — TASK 003</strong>
          <p>No lens, tool flight, brain animation, or backend events are implemented in UI-LAB-01B.</p>
        </div>
        <footer><span>Centered 80% field</span><span>Presentation-only shell</span></footer>
      </PanelStage>
    </GlassShell>
  );
}
