import { invoke } from "@tauri-apps/api/core";
import type {
  EvidenceNativeCommandResult,
  EvidenceUiCommand,
} from "@uiux/simulation/EvidenceOSSimulationContract";
import { evidenceLanePickerPolicy } from "@uiux/simulation/EvidenceOSSimulationContract";
import { callWorker } from "./backend";

type TauriWindow = Window & { __TAURI_INTERNALS__?: unknown };
type NativePickerRequest = { kind: "file" | "folder"; multiple: boolean; filters: string[] };

const PROFILE_IMAGE_FILTERS = ["bmp", "gif", "jpeg", "jpg", "png", "tif", "tiff", "webp"];

function pickerRequest(command: EvidenceUiCommand, payload: Record<string, unknown>): NativePickerRequest | null {
  if (command === "workspace.outputRoot.choose" || command === "workspace.rootHistory.add") {
    return { kind: "folder", multiple: false, filters: [] };
  }
  if (command === "profile.image.choose") {
    return { kind: "file", multiple: false, filters: PROFILE_IMAGE_FILTERS };
  }
  if (command === "character.glb.choose") {
    return { kind: "file", multiple: false, filters: ["glb"] };
  }
  if (command === "ollama.executable.choose") {
    return { kind: "file", multiple: false, filters: ["exe", "lnk"] };
  }
  if (command !== "source.path.choose") return null;

  const requestedLane = String(payload.lane_key || payload.laneKey || "custom");
  const laneKey = requestedLane.startsWith("custom_") ? "custom" : requestedLane;
  const policy = evidenceLanePickerPolicy[laneKey] || evidenceLanePickerPolicy.custom;
  const requestedKind = String(payload.picker_kind || payload.pickerKind || "");
  const folderOnly = laneKey === "local_code" || laneKey === "project_engulf";
  const kind = folderOnly || requestedKind === "folder" || policy.kind === "folder" ? "folder" : "file";
  return {
    kind,
    multiple: kind === "file" && laneKey !== "sqlite_brain",
    filters: kind === "file" ? policy.extensions.map((extension) => extension.replace(/^\./, "")) : [],
  };
}

function resultRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function explorerTitleHint(result: Record<string, unknown>) {
  const raw = String(
    result.path
    || result.folder
    || result.target
    || result.handoff_folder
    || result.handoffFolder
    || "",
  );
  return raw.split(/[\\/]/).filter(Boolean).at(-1) || "";
}

export function installAcceptedUiNativeTransport() {
  if (!(window as TauriWindow).__TAURI_INTERNALS__) return false;
  window.__EVIDENCE_OS_NATIVE_COMMAND_TRANSPORT__ = async (
    command: EvidenceUiCommand,
    payload: Record<string, unknown>,
  ): Promise<EvidenceNativeCommandResult> => {
    try {
      if (command === "shell.external.launch") {
        const result = await invoke<Record<string, unknown>>("open_chrome_profile", {
          provider: String(payload.target || ""),
        });
        return { ok: true, result };
      }
      if (command === "folder.open" && String(payload.path || "").trim()) {
        const result = await invoke<Record<string, unknown>>("open_native_folder", {
          path: String(payload.path),
        });
        return { ok: true, result };
      }

      const ownedPicker = pickerRequest(command, payload);
      let workerPayload = payload;
      if (ownedPicker) {
        const selectedPaths = await invoke<string[]>("choose_paths", ownedPicker);
        workerPayload = {
          ...payload,
          native_picker_completed: true,
          native_selected_paths: selectedPaths,
        };
      }

      const response = await callWorker(command, workerPayload);
      if (!response.ok) {
        return { ok: false, error: response.error };
      }

      const result = resultRecord(response.result);
      const status = String(result.status || "");
      if ((command === "ollama.launch" || command === "codex.launch") && status === "LAUNCHED") {
        const handoff = await invoke<Record<string, unknown>>("handoff_external_window", {
          target: command === "ollama.launch" ? "ollama" : "codex",
          minimizeEvidenceOs: true,
        });
        return { ok: true, result: { ...result, foreground_handoff: handoff } };
      }
      if (["folder.open", "package.open", "brain.codexHandoff.openFolder"].includes(command)) {
        const handoff = await invoke<Record<string, unknown>>("handoff_external_window", {
          target: "explorer",
          minimizeEvidenceOs: true,
          titleHint: explorerTitleHint(result),
        });
        return { ok: true, result: { ...result, foreground_handoff: handoff } };
      }
      if (command === "brain.telemetry.openTarget" && status === "OPENED") {
        const targetKind = String(result.target_kind || "").toLowerCase();
        const application = String(result.application || "default").toLowerCase();
        // Windows ShellExecute already chose the registered owning application
        // for non-code files. The native host locates that owner by the opened
        // filename, then applies the same minimize/maximize/foreground contract
        // used by Explorer and VS Code.
        const target = targetKind === "folder"
          ? "explorer"
          : application === "vscode" ? "vscode" : "owner";
        const handoff = await invoke<Record<string, unknown>>("handoff_external_window", {
          target,
          minimizeEvidenceOs: true,
          titleHint: explorerTitleHint(result),
        });
        return { ok: true, result: { ...result, foreground_handoff: handoff } };
      }
      return { ok: true, result: response.result };
    } catch (error) {
      return {
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  };
  return true;
}
