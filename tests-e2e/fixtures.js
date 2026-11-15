// Playwright fixtures: real backend (uvicorn + SQLite), a local test website, and the real
// NOVA extension loaded into Chromium.
import { test as base, chromium, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(here, '..');
export const API_PORT = Number(process.env.NOVA_E2E_API_PORT || 8765);
export const SITE_PORT = Number(process.env.NOVA_E2E_SITE_PORT || 5174);
export const API = `http://127.0.0.1:${API_PORT}`;
export const SITE = `http://127.0.0.1:${SITE_PORT}`;
const TZ = 'Asia/Kolkata';

function waitFor(url, timeoutMs = 30000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () =>
      fetch(url).then((r) => (r.ok ? resolve() : retry())).catch(retry);
    const retry = () => (Date.now() - start > timeoutMs ? reject(new Error(`timeout waiting for ${url}`)) : setTimeout(tick, 250));
    tick();
  });
}

function startSite() {
  const dir = path.join(here, 'fixtures', 'site');
  const server = http.createServer((req, res) => {
    const p = path.join(dir, decodeURIComponent(new URL(req.url, SITE).pathname));
    if (!p.startsWith(dir) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) {
      res.writeHead(404).end('not found');
      return;
    }
    const type = p.endsWith('.pdf') ? 'application/pdf' : 'text/html; charset=utf-8';
    res.writeHead(200, { 'Content-Type': type }).end(fs.readFileSync(p));
  });
  return new Promise((resolve) => server.listen(SITE_PORT, '127.0.0.1', () => resolve(server)));
}

function prepareExtension(tmp) {
  // Test build: same code, but the manifest also allows the e2e API port.
  const dest = path.join(tmp, 'extension');
  fs.cpSync(path.join(ROOT, 'extension'), dest, { recursive: true, filter: (src) => !src.includes(`${path.sep}test`) && !src.includes('node_modules') });
  const mf = JSON.parse(fs.readFileSync(path.join(dest, 'manifest.json'), 'utf8'));
  mf.host_permissions.push(`${API}/*`);
  fs.writeFileSync(path.join(dest, 'manifest.json'), JSON.stringify(mf, null, 2));
  return dest;
}

export const test = base.extend({
  env: [
    async ({}, use) => {
      const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'nova-e2e-'));
      const dbPath = path.join(tmp, 'e2e.db');
      const python = path.join(ROOT, 'backend', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
      const backend = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(API_PORT), '--log-level', 'warning'], {
        cwd: path.join(ROOT, 'backend'),
        env: { ...process.env, DATABASE_URL: `sqlite:///${dbPath.replace(/\\/g, '/')}`, GEMINI_API_KEY: '', GROQ_API_KEY: '', RATE_LIMIT_PER_MINUTE: '1000' },
        stdio: ['ignore', 'inherit', 'inherit'],
      });
      const site = await startSite();
      await waitFor(`${API}/api/health`);
      const extPath = prepareExtension(tmp);
      await use({ tmp, dbPath, extPath });
      backend.kill();
      site.close();
      // Browser profiles and the extension copy add up to ~100 MB per run: don't leave them behind.
      await new Promise((r) => setTimeout(r, 500));
      try {
        fs.rmSync(tmp, { recursive: true, force: true });
      } catch {
        /* a straggler process still holds a file; the OS temp cleaner gets it */
      }
    },
    { scope: 'worker' },
  ],

  context: async ({ env }, use) => {
    const context = await launchNova(env, fs.mkdtempSync(path.join(env.tmp, 'profile-')));
    await use(context);
    await context.close();
  },

  sw: async ({ context }, use) => {
    let [sw] = context.serviceWorkers();
    if (!sw) sw = await context.waitForEvent('serviceworker');
    await use(sw);
  },

  extId: async ({ sw }, use) => use(sw.url().split('/')[2]),

  // Registers a fresh user and connects the extension (as the onboarding form would).
  account: async ({ sw, context }, use, testInfo) => use(await connect(sw, context, `e2e-${testInfo.testId}`)),
});

export { expect };

export async function launchNova(env, userDir) {
  return chromium.launchPersistentContext(userDir, {
    channel: 'chromium',
    headless: !process.env.HEADED,
    timezoneId: TZ,
    acceptDownloads: true,
    args: [`--disable-extensions-except=${env.extPath}`, `--load-extension=${env.extPath}`],
  });
}

export async function serviceWorker(context) {
  return context.serviceWorkers()[0] || context.waitForEvent('serviceworker');
}

export async function connect(sw, context, label = 'e2e') {
  const email = `${label}-${Date.now()}@example.com`;
  const res = await fetch(`${API}/api/auth/register`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'Kapil', email, timezone: TZ }) });
  const { token, user } = await res.json();
  await sw.evaluate(async ({ token, user, api }) => {
    await chrome.storage.local.set({ token, user, backendUrl: api, debugOpenShadow: true, settings: { suggestions: false } });
  }, { token, user, api: API });
  for (const p of context.pages()) if (p.url().includes('/options/')) await p.close().catch(() => {});
  const call = async (p, opts = {}) => {
    const r = await fetch(API + p, { ...opts, headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', ...(opts.headers || {}) } });
    return r.status === 204 ? null : r.json();
  };
  return { token, user, email, call };
}

// Helpers ---------------------------------------------------------------------------
export async function openOrb(page) {
  const orb = page.locator('#nova-host .orb');
  await expect(orb).toBeVisible({ timeout: 10000 });
  await orb.click();
  await expect(page.locator('#nova-host .panel')).toBeVisible();
  return page.locator('#nova-host');
}

export async function askOrb(page, text) {
  const host = page.locator('#nova-host');
  const before = await host.locator('.msg.bot').count();
  await host.locator('input').fill(text);
  await host.locator('input').press('Enter');
  await expect(host.locator('.msg.bot')).toHaveCount(before + 1, { timeout: 20000 });
  return host.locator('.msg.bot').last();
}

export async function askPanel(panel, text) {
  const before = await panel.locator('.bubble.bot').count();
  await panel.locator('#chat-input').fill(text);
  await panel.locator('#chat-input').press('Enter');
  await expect(panel.locator('.bubble.bot')).toHaveCount(before + 1, { timeout: 20000 });
  return panel.locator('.bubble.bot').last();
}
