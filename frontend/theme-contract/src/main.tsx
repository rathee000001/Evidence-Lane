import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { installIsolatedBenchmarkBackendTransport } from "./benchmarkBackendTransport";
import "./theme/sqlite-glass-theme.css";

installIsolatedBenchmarkBackendTransport();

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
