import { defineConfig } from '@playwright/test';
import base from './playwright.camera-modes.config';

export default defineConfig({ ...base, testMatch: /analysis\.spec\.ts/ });
