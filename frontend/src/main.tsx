import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { installAcceptedUiNativeTransport } from "./nativeAcceptedUiTransport";
import { installIsolatedBenchmarkBackendTransport } from "@uiux/benchmarkBackendTransport";
import "@uiux/theme/sqlite-glass-theme.css";

const isolatedBenchmarkRequested = new URLSearchParams(window.location.search)
  .get("benchmark_backend") === "1";
if (isolatedBenchmarkRequested) installIsolatedBenchmarkBackendTransport();
else installAcceptedUiNativeTransport();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
