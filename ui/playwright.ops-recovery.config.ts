import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: 'ops-recovery.spec.ts',
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:5194', channel: 'chrome', viewport: { width: 1024, height: 600 } },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5194 --strictPort',
    url: 'http://127.0.0.1:5194',
    reuseExistingServer: false,
  },
});
