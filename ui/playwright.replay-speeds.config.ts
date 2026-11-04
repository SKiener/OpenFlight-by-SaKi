import { defineConfig } from '@playwright/test';
import cameraConfig from './playwright.camera-modes.config';

export default defineConfig({
  ...cameraConfig,
  testMatch: /camera-replay\.spec\.ts/,
});
