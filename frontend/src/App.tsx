import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useEffect } from "react";
import { EvidenceOSCockpitSimulation } from "@uiux/components/EvidenceOSCockpitSimulation";
import { callWorker } from "./backend";

const FULL_APP_FRONTEND_CONTRACT = "T023_FULL_APP_FRONTEND_V2";

type FullAppBackendPing = {
  contract: "T023_FULL_APP_BACKEND_V2";
  workspace_required: false;
  pid: number;
  frozen: boolean;
  packaging_mode: string;
};

export function App() {
  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void listen<Record<string, unknown>>("worker-event", (event) => {
      window.dispatchEvent(new CustomEvent("evidence-os-worker-event", { detail: event.payload }));
    }).then((release) => {
      if (disposed) release();
      else unlisten = release;
    });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function proveEmbeddedFullAppBoot() {
      const response = await callWorker<FullAppBackendPing>("system.ping");
      if (cancelled || !response.ok || !response.result) return;
      const backend = response.result;
      await invoke("record_full_app_boot_probe", {
        probe: {
          schema: "T023_FULL_APP_BOOT_PROBE_V1",
          frontend_contract: FULL_APP_FRONTEND_CONTRACT,
          backend_contract: backend.contract,
          backend_pid: backend.pid,
          backend_frozen: backend.frozen,
          backend_packaging_mode: backend.packaging_mode,
          workspace_required_for_probe: backend.workspace_required,
          rendered_frontend_url: window.location.href,
          rendered_at_utc: new Date().toISOString(),
        },
      });
    }
    void proveEmbeddedFullAppBoot();
    return () => { cancelled = true; };
  }, []);

  return <EvidenceOSCockpitSimulation />;
}

export default App;
