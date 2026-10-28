// Two browser suites (Chromium):
// - pixel: the built console with the prototype's sample data, screenshot-compared with
//   docs/prototype/agent-console.html page by page (served by `vite preview`, API mocked).
// - smoke: the real agentd on fakes (`agentd run --fake --live-recipes`), approving a drafted reply
//   until the task moves to Waiting on others.
// Run `npm run build` first; then `npm run test:e2e`.
import { rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { defineConfig } from "@playwright/test";

export const SMOKE_DATA = join(tmpdir(), "agentd-console-smoke");
if (!process.env.TEST_WORKER_INDEX) rmSync(SMOKE_DATA, { recursive: true, force: true });

const executablePath = process.env.PLAYWRIGHT_CHROMIUM || undefined;

export default defineConfig({
  testDir: "tests",
  timeout: 90_000,
  workers: 1,
  reporter: [["list"]],
  snapshotPathTemplate: ".pixel/{arg}{ext}",
  use: { browserName: "chromium", launchOptions: { executablePath }, viewport: { width: 1440, height: 1000 } },
  projects: [
    { name: "pixel", testMatch: /pixel\.spec\.ts/, use: { baseURL: "http://127.0.0.1:4173" } },
    { name: "smoke", testMatch: /smoke\.spec\.ts/, use: { baseURL: "http://127.0.0.1:8765" } },
  ],
  webServer: [
    {
      command: "npx vite preview --port 4173 --strictPort --host 127.0.0.1",
      url: "http://127.0.0.1:4173",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `uv run agentd run --fake --live-recipes --data-dir "${SMOKE_DATA}"`,
      cwd: "../backend",
      url: "http://127.0.0.1:8765/",
      reuseExistingServer: false,
      timeout: 180_000,
      stdout: "ignore",
    },
  ],
});
