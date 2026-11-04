import { defineConfig } from '@playwright/test';

const HOST = '127.0.0.1';

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: /companion(-pairing)?\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: {
    // Real Chrome plays the mock server's H.264 replay; bundled Chromium cannot.
    channel: 'chrome',
    baseURL: `http://${HOST}:5173`,
    hasTouch: true,
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'iphone-portrait', use: { viewport: { width: 390, height: 844 }, isMobile: true } },
    { name: 'ipad-landscape', use: { viewport: { width: 1024, height: 768 } } },
  ],
  webServer: [
    {
      command: 'npm run mock-server',
      url: `http://${HOST}:8080/health`,
      reuseExistingServer: false,
    },
    {
      command: `npm run dev -- --host ${HOST} --port 5173 --strictPort --mode test`,
      url: `http://${HOST}:5173`,
      reuseExistingServer: false,
    },
  ],
});
