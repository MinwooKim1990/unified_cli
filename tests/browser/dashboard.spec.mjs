import { test, expect } from "@playwright/test";
import axe from "axe-core";
import { spawn } from "node:child_process";
import { once } from "node:events";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const READY_TIMEOUT_MS = 10_000;
const SHUTDOWN_TIMEOUT_MS = 5_000;
let fixture;
let baseUrl;
let bootstrapToken;

function readReadyLine(child) {
  return new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error("browser fixture timed out")), READY_TIMEOUT_MS);
    const finish = (error, value) => {
      clearTimeout(timer);
      child.stdout.off("data", onData);
      child.off("exit", onExit);
      if (error) reject(error); else resolve(value);
    };
    const onData = (chunk) => {
      output += String(chunk);
      const newline = output.indexOf("\n");
      if (newline < 0) return;
      const line = output.slice(0, newline).trim();
      const match = /^READY (\d+) ([A-Za-z0-9_-]+)$/.exec(line);
      if (!match) finish(new Error("browser fixture did not produce a READY line"));
      else finish(null, { port: Number(match[1]), token: match[2] });
    };
    const onExit = (code) => finish(new Error(`browser fixture exited early (${code})`));
    child.stdout.on("data", onData);
    child.once("exit", onExit);
  });
}

async function stopFixture(child) {
  if (!child || child.exitCode !== null) return;
  child.kill("SIGTERM");
  await waitForExit(child);
  if (child.exitCode !== null) return;
  child.kill("SIGKILL");
  await waitForExit(child);
}

function waitForExit(child) {
  if (child.exitCode !== null) return Promise.resolve();
  return Promise.race([
    once(child, "exit"),
    new Promise((resolve) => setTimeout(resolve, SHUTDOWN_TIMEOUT_MS))
  ]);
}

test.beforeAll(async () => {
  fixture = spawn(process.env.PYTHON || "python", ["tests/browser/fake_server.py"], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: path.join(root, "src") },
    stdio: ["ignore", "pipe", "pipe"]
  });
  const ready = await readReadyLine(fixture);
  baseUrl = `http://127.0.0.1:${ready.port}`;
  bootstrapToken = ready.token;
});

test.afterAll(async () => stopFixture(fixture));

test("malformed bootstrap schema is fail-closed", async ({ page }) => {
  await page.route("**/api/ui/v1/bootstrap", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        version: 1, mode: "manage", manage: true,
        authenticated: true, csrf_token: "short"
      })
    });
  });
  await page.goto(`${baseUrl}/dashboard`);
  await expect(page.locator("#read-only-banner")).toBeVisible();
  await expect(page.locator("#overview-mode")).toHaveText(/read-only|읽기 전용/i);
});

test("managed dashboard is accessible, localized, and console-clean", async ({ page }, testInfo) => {
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  const responses = [];
  page.on("response", (response) => responses.push(response));

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`${baseUrl}/dashboard#bootstrap=${encodeURIComponent(bootstrapToken)}`);
  await expect(page.locator("#main-content")).toBeVisible();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.locator("#overview-mode")).toHaveText("Manage mode");
  await expect(page.locator("#overview-provider-list .badge")).toHaveCount(3);
  await expect(page.locator("#overview-provider-list .badge").filter({ hasText: "Not verified" })).toHaveCount(3);
  await expect(page.locator("#overview-provider-list .badge").filter({ hasText: "Unavailable" })).toHaveCount(0);
  await page.reload();
  await expect(page.locator("#overview-mode")).toHaveText("Manage mode");
  await page.keyboard.press("Tab");
  await expect(page.locator(".skip-link")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();

  await page.getByRole("button", { name: /settings/i }).click();
  await page.getByLabel(/language/i).selectOption("ko");
  await expect(page.locator("html")).toHaveAttribute("lang", "ko");
  await page.getByLabel(/언어/i).selectOption("en");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.locator("#connection-label")).toHaveText("Connected");

  await page.getByRole("button", { name: /chat/i }).click();
  const chatProviders = await page.locator("#chat-provider option").evaluateAll(
    (options) => options.map((option) => option.value).filter(Boolean)
  );
  expect(chatProviders.every((provider) => provider === "claude" || provider === "codex")).toBeTruthy();
  await expect(page.locator("#chat-permission")).toHaveValue("read_only");
  await page.locator("#image-picker").focus();
  await expect(page.locator("#image-picker")).toBeFocused();
  const fileFocusVisible = await page.locator(".file-button").evaluate(
    (label) => getComputedStyle(label).outlineStyle !== "none"
  );
  expect(fileFocusVisible).toBeTruthy();

  await page.getByRole("button", { name: /settings/i }).click();
  await page.locator("#setting-web").check();
  await page.locator("#settings-form").evaluate((form) => form.requestSubmit());
  await expect(page.locator("#settings-status")).toHaveText(/settings saved/i);

  if (testInfo.project.name === "smoke-360") {
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
    expect(fits).toBeTruthy();
  }

  if (testInfo.project.name === "axe-managed") {
    await page.addScriptTag({ content: axe.source });
    const results = await page.evaluate(() => window.axe.run(document));
    expect(results.violations).toEqual([]);
  }
  expect(consoleErrors).toEqual([]);
  for (const response of responses.filter((item) => item.url().includes("/dashboard"))) {
    expect(response.headers()["content-security-policy"]).toContain("default-src 'self'");
  }
  await testInfo.attach(`dashboard-${testInfo.project.name}.png`, {
    body: await page.screenshot({ fullPage: true }), contentType: "image/png"
  });
});
