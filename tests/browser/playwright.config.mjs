import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "dashboard.spec.mjs",
  timeout: 30_000,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  use: { trace: "retain-on-failure" },
  projects: [
    { name: "axe-managed", use: { viewport: { width: 1440, height: 900 }, bypassCSP: true } },
    { name: "smoke-1440", use: { viewport: { width: 1440, height: 900 } } },
    { name: "smoke-768", use: { viewport: { width: 768, height: 1024 } } },
    { name: "smoke-360", use: { viewport: { width: 360, height: 800 } } }
  ]
});
