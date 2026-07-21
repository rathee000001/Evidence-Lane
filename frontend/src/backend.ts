import { invoke } from "@tauri-apps/api/core";

export type WorkerResponse<T = unknown> = {
  type: "response";
  id: string;
  ok: boolean;
  result?: T;
  error?: string;
};

function requestId() {
  return `req_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

export async function callWorker<T = unknown>(
  command: string,
  payload: Record<string, unknown> = {},
): Promise<WorkerResponse<T>> {
  const request = { id: requestId(), command, payload };
  try {
    return (await invoke("worker_command", { request })) as WorkerResponse<T>;
  } catch (error) {
    return {
      type: "response",
      id: request.id,
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}
