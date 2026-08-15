import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

const relayUrl = process.env.VITE_RELAY_URL ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: relayUrl, changeOrigin: true },
      "/healthz": { target: relayUrl, changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["tests/e2e/**"],
  },
});
