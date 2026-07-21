import type {
  EvidenceNativeCommandResult,
  EvidenceUiCommand,
} from "./simulation/EvidenceOSSimulationContract";

const BENCHMARK_QUERY_VALUE = "1";
const BENCHMARK_BASE = "http://127.0.0.1:4191/__evidence_lane_benchmark__";

type WorkerEnvelope = {
  type?: string;
  event?: string;
  request_id?: string;
  payload?: Record<string, unknown>;
};

export function installIsolatedBenchmarkBackendTransport(): void {
  const query = new URLSearchParams(window.location.search);
  if (query.get("benchmark_backend") !== BENCHMARK_QUERY_VALUE) return;

  document.documentElement.dataset.backendTransport = "isolated-chrome-benchmark";
  document.documentElement.dataset.productionAuthority = "false";

  const eventSource = new EventSource(`${BENCHMARK_BASE}/events`);
  eventSource.addEventListener("open", () => {
    document.documentElement.dataset.backendEventStream = "connected";
  });
  eventSource.addEventListener("error", () => {
    document.documentElement.dataset.backendEventStream = "disconnected";
  });
  eventSource.addEventListener("message", (event) => {
    try {
      const envelope = JSON.parse(event.data) as WorkerEnvelope;
      // Keep both the event name and its complete nested task payload intact.
      // Every cockpit surface consumes the same authoritative worker envelope.
      window.dispatchEvent(new CustomEvent("evidence-os-worker-event", { detail: envelope }));
      window.dispatchEvent(new CustomEvent("evidence-os-worker-envelope", { detail: envelope }));
    } catch {
      const failureEnvelope: WorkerEnvelope = {
        type: "event",
        event: "benchmark.transport.error",
        payload: { status: "failed", error_code: "INVALID_BENCHMARK_EVENT" },
      };
      window.dispatchEvent(new CustomEvent("evidence-os-worker-event", { detail: failureEnvelope }));
      window.dispatchEvent(new CustomEvent("evidence-os-worker-envelope", { detail: failureEnvelope }));
    }
  });

  window.__EVIDENCE_OS_NATIVE_COMMAND_TRANSPORT__ = async (
    command: EvidenceUiCommand,
    payload: Record<string, unknown>,
  ): Promise<EvidenceNativeCommandResult> => {
    const response = await fetch(`${BENCHMARK_BASE}/command`, {
      method: "POST",
      mode: "cors",
      cache: "no-store",
      credentials: "omit",
      headers: {
        "Content-Type": "application/json",
        "X-Evidence-Lane-Benchmark": "t023-chrome-comparison",
      },
      body: JSON.stringify({ command, payload }),
    });
    const body = await response.json() as EvidenceNativeCommandResult;
    if (!response.ok) {
      return { ok: false, error: body.error || `BENCHMARK_HTTP_${response.status}` };
    }
    return body;
  };

  window.addEventListener("beforeunload", () => eventSource.close(), { once: true });
}
