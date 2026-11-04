import { defineConfig, devices } from '@playwright/test';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  PAIRING_SECRET_PATH_ENV,
  PROFILES_PATH_ENV,
  WEATHER_PATH_ENV,
  uniqueE2eProfilesPath,
} from './tests/e2e/isolateProfilesPath';

const PORT = 5173;
const HOST = '127.0.0.1';
const BASE_URL = `http://${HOST}:${PORT}`;
const CONFIG_DIR = fileURLToPath(new URL('.', import.meta.url));
// The demo mode adds a shot every 20 s; the end-to-end tests cannot wait for that.
const BACKEND_ARGS = `--mock --host ${HOST} --web-port 8080 --no-logging --demo-interval-s 2 --hotspot-ssid TestNet --hotspot-password e2e-dummy-pass-1234`;
const BACKEND_COMMAND = `uv run openflight-server ${BACKEND_ARGS}`;

const E2E_PROFILES_PATH = uniqueE2eProfilesPath();

function backendEnv(): { [key: string]: string } {
  const env: { [key: string]: string } = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value !== undefined) env[key] = value;
  }
  env[PROFILES_PATH_ENV] = E2E_PROFILES_PATH;
  env[PAIRING_SECRET_PATH_ENV] = join(dirname(E2E_PROFILES_PATH), 'pairing-secret');
  env[WEATHER_PATH_ENV] = join(dirname(E2E_PROFILES_PATH), 'weather.json');
  return env;
}

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: '**/*.spec.ts',
  // Companion specs need the Node mock's camera replays; see playwright.companion.config.ts.
  testIgnore: /companion(-pairing)?\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? [['html', { outputFolder: 'playwright-report', open: 'never' }], ['github']] : 'list',
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  webServer: [
    {
      command: BACKEND_COMMAND,
      url: `http://${HOST}:8080`,
      reuseExistingServer: false,
      cwd: fileURLToPath(new URL('..', import.meta.url)),
      env: backendEnv(),
    },
    {
      command: `npm run dev -- --host ${HOST} --port ${PORT} --mode test`,
      url: BASE_URL,
      reuseExistingServer: !process.env.CI,
      cwd: CONFIG_DIR,
    },
  ],
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
