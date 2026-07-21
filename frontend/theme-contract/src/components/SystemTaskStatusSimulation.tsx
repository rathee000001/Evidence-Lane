import { useEffect, useState } from "react";
import { SystemTaskStatus } from "./SystemTaskStatus";

export function SystemTaskStatusSimulation() {
  const [progress, setProgress] = useState(48);
  const [event, setEvent] = useState("UI-only action preview");
  useEffect(() => { const timer = window.setInterval(() => setProgress((value) => value >= 100 ? 0 : value + 2), 700); return () => window.clearInterval(timer); }, []);
  const complete = Math.floor(progress / 20);
  return (
    <main className="task-process-demo">
      <SystemTaskStatus
        progress={progress}
        task="Building SQLite evidence index"
        detail="Deterministic demo fields; no backend connection."
        running={progress < 100 ? 2 : 0}
        queued={Math.max(0, 4 - complete)}
        completed={complete}
        elapsed={`00:${String(Math.floor(progress * .72)).padStart(2, "0")}`}
        stage="Index"
        status={progress >= 100 ? "completed" : "running"}
        onAction={(action) => setEvent(`${action} preview selected`)}
        queue={[
          { id: "1", label: "Parse source lane", state: progress > 22 ? "complete" : "running" },
          { id: "2", label: "Write semantic index", state: progress > 22 ? "running" : "queued" },
          { id: "3", label: "Seal brain snapshot", state: progress > 78 ? "running" : "queued" },
        ]}
      />
      <span className="task-process-demo__note">UI simulation · {event}</span>
    </main>
  );
}
