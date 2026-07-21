import { CollapsedSidePanelShell, EvidenceHeaderSimulation, EvidenceOSCockpitSimulation, ExpandedSidePanelShell, HeaderShell, MetricsClusterSimulation, MetricsShell, PromptBarShell, SystemTaskStatusSimulation, TaskStatusShell, ToolchainFlowSimulation, ToolchainShell, WorkspaceShell } from "./components";

export function App() {
  const params = new URLSearchParams(window.location.search);
  const demo = params.get("demo");
  if (!demo || demo === "t023" || demo === "cockpit") return <EvidenceOSCockpitSimulation />;
  if (demo === "metrics") return <MetricsClusterSimulation />;
  if (demo === "task-status") return <SystemTaskStatusSimulation />;
  if (demo === "header") return <EvidenceHeaderSimulation />;
  if (demo === "tool-flow") return <ToolchainFlowSimulation />;
  const expanded = params.get("mode") === "expanded";
  return (
    <main className="glass-simulation">
      <WorkspaceShell sideMode={expanded ? "expanded" : "collapsed"}>
        {expanded ? <ExpandedSidePanelShell /> : <CollapsedSidePanelShell />}
        <div className="evidence-shell-grid">
          <HeaderShell />
          <div className="evidence-shell-grid__upper"><MetricsShell /><TaskStatusShell /></div>
          <ToolchainShell />
          <PromptBarShell />
        </div>
      </WorkspaceShell>
    </main>
  );
}

export default App;
