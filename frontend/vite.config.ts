import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

const themeContractRoot = path.resolve(__dirname, "theme-contract");

export default defineConfig({
  plugins: [react()],
  publicDir: path.resolve(themeContractRoot, "public"),
  resolve: {
    alias: {
      "@uiux": path.resolve(themeContractRoot, "src"),
    },
  },
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    port: 1430,
    strictPort: true,
  },
});
