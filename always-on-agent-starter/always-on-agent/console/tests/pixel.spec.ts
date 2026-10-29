// Pixel comparison with the prototype (prompt 1.16: "match its pages"). Both are rendered at
// 1440 x 1000 with the same sample data; the visible viewport of each page is compared.
import { fileURLToPath } from "node:url";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { expect, test, type Page } from "@playwright/test";

import { routeApi } from "../src/test/mockApi";

const SOURCE = fileURLToPath(new URL("../../docs/prototype/agent-console.html", import.meta.url));
// The prototype file has no doctype (its host adds the page skeleton), so a browser opening it
// directly uses quirks mode, which changes table and line heights. Compare in standards mode, as
// hosted and as the console renders.
const PROTOTYPE = fileURLToPath(new URL("../.pixel/prototype-standards.html", import.meta.url));
mkdirSync(dirname(PROTOTYPE), { recursive: true });
writeFileSync(PROTOTYPE, `<!doctype html><html lang="en"><head><meta charset="utf-8">${readFileSync(SOURCE, "utf8")}`);

// Measured at 1440 x 1000 (2026-10-29): tasks and connectors 0 px, activity 1 px, the rest 150-980 px of
// 1.44 M (antialiasing of a few glyphs and one sentence of truthful copy). The limit is 0.1 %.
const PAGES = ["brief", "tasks", "task-inc30114872", "approvals", "recipes", "activity", "memory", "connectors", "prefs"];
const LIMIT = 0.001;

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/stream") {
      await route.fulfill({ status: 200, contentType: "text/event-stream", body: "retry: 600000\n\n" });
      return;
    }
    const body = request.postData() ? JSON.parse(request.postData()!) : undefined;
    const { status, json } = routeApi(request.method(), url.pathname + url.search, body);
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(json) });
  });
}

for (const hash of PAGES) {
  test(`${hash} matches the prototype`, async ({ page, browser }, info) => {
    const proto = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await proto.goto(`file://${PROTOTYPE}#${hash}`);
    await proto.addStyleTag({ content: "body{margin:0} .toast{display:none}" });
    await proto.evaluate(() => document.fonts.ready);
    const expected = await proto.screenshot({ mask: [proto.locator(".side-foot")] });
    const path = info.snapshotPath(`${hash}.png`);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, expected);

    await mockApi(page);
    await page.goto(`/#${hash}`);
    await page.locator("main .page h1").first().waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(300);
    const actual = await page.screenshot({ mask: [page.locator(".side-foot")] });
    expect(actual).toMatchSnapshot(`${hash}.png`, { maxDiffPixelRatio: process.env.PIXEL_REPORT ? 0 : LIMIT, threshold: 0.2 });
  });
}

// Phone width (prompt 1.16: "works at 400 px wide"): the nav becomes a scrolling top bar, as in the prototype.
for (const hash of ["brief", "approvals", "prefs"]) {
  test(`${hash} at 400 px matches the prototype and does not scroll sideways`, async ({ browser }, info) => {
    const size = { width: 400, height: 900 };
    const proto = await browser.newPage({ viewport: size });
    await proto.goto(`file://${PROTOTYPE}#${hash}`);
    await proto.addStyleTag({ content: "body{margin:0} .toast{display:none}" });
    await proto.evaluate(() => document.fonts.ready);
    const path = info.snapshotPath(`${hash}-400.png`);
    writeFileSync(path, await proto.screenshot());

    const page = await browser.newPage({ viewport: size, baseURL: "http://127.0.0.1:4173" });
    await mockApi(page);
    await page.goto(`/#${hash}`);
    await page.locator("main .page h1").first().waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(300);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    expect(await page.screenshot()).toMatchSnapshot(`${hash}-400.png`, {
      maxDiffPixelRatio: process.env.PIXEL_REPORT ? 0 : LIMIT * 5,
      threshold: 0.2,
    });
  });
}
