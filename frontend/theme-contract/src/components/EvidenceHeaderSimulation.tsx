import { useState } from "react";
import { EvidenceHeaderShell } from "./EvidenceHeaderShell";

export function EvidenceHeaderSimulation() {
  const [event, setEvent] = useState("Ready for App Builder callbacks");
  return (
    <main className="evidence-header-demo">
      <EvidenceHeaderShell activeModule="sqlite" onModuleSwitch={(module) => setEvent(`${module} callback fired`)} onEscapeAdmin={() => setEvent("Escape to Admin Cockpit callback fired")} />
      <output className="evidence-header-demo__event">{event}</output>
    </main>
  );
}
