// End-to-end tests against the whole stack, in a real browser (ADR 0015).
//
//   make up && make simulate FAULTS=0   # the stack and a fleet that behaves
//   make web-dev                        # the app on :5173, proxying /api to the BFF
//   make e2e
//
// Nothing is mocked: Keycloak signs people in, the BFF holds the session, devices answer
// over MQTT, and the floor plan updates over the live WebSocket.

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "tests",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.WEB_URL ?? "http://localhost:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
