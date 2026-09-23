import { defineConfig, devices } from "@playwright/test";

/**
 * 真实联调 E2E：不启动本地 dev server，直接访问 docker compose 暴露的 web 服务，
 * /api 由容器内 nginx 反代到 api 服务。
 *
 * WEB_BASE_URL 默认 http://localhost:8080（compose 默认 WEB_PORT）。
 */
export default defineConfig({
  testDir: "./e2e",
  // 单用例默认 60s：低端 CI（如 arm64 容器内的 headless shell）单次提交较慢；
  // 多次串行提交的用例会在文件内用 test.setTimeout 再放宽。
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.WEB_BASE_URL ?? "http://localhost:8080",
    trace: "retain-on-failure",
    // Docker（含验收容器）内以 root 运行时需要关闭沙箱
    launchOptions: {
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
    },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
