// Smoke test (prompt 1.16): the real agentd on fakes. Sign in with the one-time link from
// `agentd console`, approve the reply the agent drafted (save the draft, then send it) and see the
// task move from Needs you to Waiting on others, with live updates from /api/stream.
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

import { SMOKE_DATA } from "../playwright.config";

const BACKEND = fileURLToPath(new URL("../../backend", import.meta.url));

function loginLink(): string {
  const out = execFileSync("uv", ["run", "agentd", "console", "--data-dir", SMOKE_DATA], {
    cwd: BACKEND,
    encoding: "utf8",
  });
  const link = out.match(/http:\/\/127\.0\.0\.1:\d+\/api\/auth\/login\?code=\S+/);
  if (!link) throw new Error(`no link in: ${out}`);
  return link[0];
}

test("approve a drafted reply and see the task move", async ({ page }) => {
  // Without the session the API refuses and the console says how to sign in.
  await page.goto("/#brief");
  await expect(page.getByText("Open the console from its link")).toBeVisible();

  await page.goto(loginLink());
  await expect(page).toHaveURL(/#brief$/);
  await expect(page.locator("#statusText")).toHaveText(/Running/);

  await page.goto("/#tasks");
  const needs = page.getByTestId("col-needs");
  const card = needs.locator("a.card", { has: page.locator(".mode.Ask") }).first();
  await expect(card).toBeVisible({ timeout: 30_000 });
  const id = (await card.locator(".mono").first().textContent())!.trim();

  await card.click();
  await expect(page.locator("h1")).toContainText(id);
  const approval = page.locator(".approval");
  await expect(approval).toContainText("Email (draft in Outlook)");
  await approval.getByRole("button", { name: "Approve" }).click();
  await expect(page.locator("#toast")).toContainText("Approved");

  // The worker saves the draft, then asks before sending: a new card arrives over the stream.
  await expect(approval).toContainText("Email to", { timeout: 30_000 });
  await approval.getByRole("button", { name: "Approve" }).click();
  await expect(page.locator(".pill")).toHaveText("Waiting on others", { timeout: 30_000 });
  await expect(page.locator(".thread")).toContainText("Sent the email.");

  await page.goto("/#tasks");
  await expect(page.getByTestId("col-waiting").getByText(id)).toBeVisible();
  await expect(page.getByTestId("col-needs").getByText(id)).toHaveCount(0);

  await page.goto("/#activity");
  await expect(page.locator("tbody")).toContainText("send_mail");
});
