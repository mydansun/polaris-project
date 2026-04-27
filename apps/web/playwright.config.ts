import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the web e2e suite.
 *
 * Targets the live compose stack via traefik on POLARIS_DOMAIN — same
 * URL a real browser uses, same TLS path, real Let's Encrypt cert.
 * No vite dev server is started here; the compose stack must already
 * be running (`./scripts/up.py`).
 *
 * Browser binary: the bundled chromium that the playwright MCP server
 * also uses.  Pinned via PLAYWRIGHT_BROWSERS_PATH so a `pnpm install`
 * doesn't drag in a duplicate copy.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,                         // login-state shared
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: process.env.POLARIS_E2E_BASE_URL ?? "https://polaris-dev.xyz",
    trace: "on-first-retry",
    ignoreHTTPSErrors: false,                   // we have a real LE cert
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
