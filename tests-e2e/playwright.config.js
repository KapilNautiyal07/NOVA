import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: /.*\.spec\.js/,
  timeout: 60000,
  workers: 1, // one backend + one extension profile per worker
  retries: 0,
  reporter: [['list']],
  use: { trace: 'retain-on-failure' },
});
