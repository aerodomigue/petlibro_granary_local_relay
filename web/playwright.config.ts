import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  webServer: {
    command: "npm run build && npm run preview -- --host 127.0.0.1 --port 5173",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: !process.env.CI,
  },
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:5173",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile-360", use: { browserName: "chromium", hasTouch: true, isMobile: true, viewport: { width: 360, height: 800 } } },
    { name: "mobile-390", use: { ...devices["iPhone 13"], browserName: "chromium" } },
    { name: "mobile-430", use: { browserName: "chromium", hasTouch: true, isMobile: true, viewport: { width: 430, height: 932 } } },
    { name: "tablet-768", use: { browserName: "chromium", hasTouch: true, isMobile: true, viewport: { width: 768, height: 1024 } } },
  ],
});
