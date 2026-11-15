// Google Calendar end-to-end through the real extension UI: permission prompt → "Connect Google
// Calendar" button → OAuth consent (Google's page is intercepted in the browser; token exchange
// and the Calendar API hit a local fake Google) → NOVA continues the conversation → confirmation
// → event created with the right local time → link to the event.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium, expect, test } from '@playwright/test';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PORT = 8772;
const GOOGLE_PORT = 8771;
const SITE_PORT = 8773;
const API = `http://127.0.0.1:${PORT}`;
const GOOGLE = `http://127.0.0.1:${GOOGLE_PORT}`;
const TZ = 'Asia/Kolkata';
const SCOPE = 'https://www.googleapis.com/auth/calendar.events';

test.setTimeout(90000);

function listen(server, port) {
  return new Promise((r) => server.listen(port, '127.0.0.1', r));
}

async function startStack() {
  const google = { tokenRequests: [], events: [], revoked: [] };
  // "Google's consent screen": record what NOVA asked for, then answer like Google would.
  const consent = { requests: [], answer: 'allow' };
  const fake = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const body = Buffer.concat(chunks).toString();
      const json = (code, obj) => res.writeHead(code, { 'Content-Type': 'application/json' }).end(JSON.stringify(obj));
      const url = new URL(req.url, GOOGLE);
      if (url.pathname === '/o/oauth2/v2/auth') {
        consent.requests.push(Object.fromEntries(url.searchParams));
        const back = new URL(url.searchParams.get('redirect_uri'));
        back.searchParams.set('state', url.searchParams.get('state'));
        if (consent.answer === 'allow') {
          back.searchParams.set('code', 'mock-code');
          back.searchParams.set('scope', SCOPE);
        } else back.searchParams.set('error', 'access_denied');
        return res.writeHead(302, { Location: back.href }).end();
      }
      if (url.pathname === '/token') {
        const form = Object.fromEntries(new URLSearchParams(body));
        google.tokenRequests.push(form);
        if (form.grant_type === 'authorization_code' && form.code === 'mock-code' && form.code_verifier && form.client_secret === 'test-secret')
          return json(200, { access_token: 'at-1', refresh_token: 'rt-1', expires_in: 3600, scope: SCOPE, token_type: 'Bearer' });
        return json(400, { error: 'invalid_grant' });
      }
      if (url.pathname === '/revoke') {
        google.revoked.push(url.searchParams.get('token'));
        return json(200, {});
      }
      if (url.pathname === '/calendar/v3/calendars/primary/events') {
        if (req.headers.authorization !== 'Bearer at-1') return json(401, { error: 'unauthorized' });
        if (req.method === 'GET') return json(200, { summary: 'kapil@example.com', items: [] });
        const ev = JSON.parse(body);
        google.events.push(ev);
        return json(200, { ...ev, htmlLink: `https://calendar.google.com/calendar/event?eid=${ev.id}` });
      }
      json(404, {});
    });
  });
  await listen(fake, GOOGLE_PORT);
  const site = http.createServer((_q, res) => res.writeHead(200, { 'Content-Type': 'text/html' }).end('<!doctype html><title>Project board</title><h1>Sprint</h1>'));
  await listen(site, SITE_PORT);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'nova-cal-'));
  const py = path.join(ROOT, 'backend', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const server = spawn(py, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(PORT), '--log-level', 'warning'], {
    cwd: path.join(ROOT, 'backend'),
    env: {
      ...process.env,
      DATABASE_URL: `sqlite:///${path.join(tmp, 'c.db').split(path.sep).join('/')}`,
      GEMINI_API_KEY: '', GROQ_API_KEY: '', RATE_LIMIT_PER_MINUTE: '1000', ENVIRONMENT: 'development',
      GOOGLE_CLIENT_ID: 'test-client.apps.googleusercontent.com', GOOGLE_CLIENT_SECRET: 'test-secret', TOKEN_ENCRYPTION_KEY: '',
      GOOGLE_REDIRECT_URI: `${API}/api/integrations/google/callback`,
      GOOGLE_AUTH_URL: `${GOOGLE}/o/oauth2/v2/auth`, GOOGLE_TOKEN_URL: `${GOOGLE}/token`, GOOGLE_REVOKE_URL: `${GOOGLE}/revoke`, GOOGLE_CALENDAR_API: `${GOOGLE}/calendar/v3`,
    },
    stdio: ['ignore', 'inherit', 'inherit'],
  });
  await expect.poll(() => fetch(`${API}/api/health`).then((r) => r.ok).catch(() => false), { timeout: 30000 }).toBe(true);

  const ext = path.join(tmp, 'extension');
  fs.cpSync(path.join(ROOT, 'extension'), ext, { recursive: true, filter: (s) => !s.includes(`${path.sep}test`) && !s.includes('node_modules') && !s.includes(`${path.sep}models`) });
  const mf = JSON.parse(fs.readFileSync(path.join(ext, 'manifest.json'), 'utf8'));
  mf.host_permissions.push(`${API}/*`);
  fs.writeFileSync(path.join(ext, 'manifest.json'), JSON.stringify(mf, null, 2));
  const context = await chromium.launchPersistentContext(fs.mkdtempSync(path.join(tmp, 'profile-')), {
    channel: 'chromium', headless: !process.env.HEADED, timezoneId: TZ,
    args: [`--disable-extensions-except=${ext}`, `--load-extension=${ext}`],
  });

  const sw = context.serviceWorkers()[0] || (await context.waitForEvent('serviceworker'));
  const reg = await fetch(`${API}/api/auth/register`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'Kapil', email: `cal-${Date.now()}@example.com`, timezone: TZ }) });
  const { token, user } = await reg.json();
  for (const p of context.pages()) if (p.url().includes('/options/')) await p.close().catch(() => {});
  await sw.evaluate(({ token, user, api }) => chrome.storage.local.set({ token, user, backendUrl: api, debugOpenShadow: true, settings: { suggestions: false } }), { token, user, api: API });

  const stop = async () => {
    await context.close();
    server.kill();
    fake.close();
    site.close();
    await new Promise((r) => setTimeout(r, 500));
    try {
      fs.rmSync(tmp, { recursive: true, force: true });
    } catch {
      /* a straggler holds a file */
    }
  };
  return { google, consent, context, sw, stop };
}

async function say(host, text) {
  const before = await host.locator('.msg.bot').count();
  await host.locator('input').fill(text);
  await host.locator('input').press('Enter');
  await expect(host.locator('.msg.bot')).toHaveCount(before + 1, { timeout: 20000 });
  return host.locator('.msg.bot').last();
}

test('schedule a meeting: permission → connect → details → confirm → event on Google Calendar', async () => {
  const stack = await startStack();
  try {
    const page = await stack.context.newPage();
    await page.goto(`http://127.0.0.1:${SITE_PORT}/`);
    const host = page.locator('#nova-host');
    await host.locator('.orb').click();

    // Not connected: NOVA explains why it needs permission and shows a button. Nothing is opened yet.
    let reply = await say(host, 'Schedule my meeting for tomorrow');
    await expect(reply).toContainText('I need your permission first');
    const card = host.locator('.card', { hasText: 'Google’s sign-in page' });
    await expect(card.getByRole('button', { name: 'Connect Google Calendar' })).toBeVisible();
    expect(stack.consent.requests).toHaveLength(0);

    // Click → Google's consent page opens in a new tab (only now).
    const [consentTab] = await Promise.all([stack.context.waitForEvent('page'), card.getByRole('button', { name: 'Connect Google Calendar' }).click()]);
    await expect.poll(() => stack.consent.requests.length).toBe(1);
    const q = stack.consent.requests[0];
    expect(q.scope).toBe(SCOPE); // minimum scope
    expect(q.code_challenge_method).toBe('S256');
    expect(q.access_type).toBe('offline');
    expect(JSON.stringify(q)).not.toContain('test-secret'); // the client secret never reaches the browser
    await expect(consentTab.locator('h1')).toHaveText('Google Calendar connected', { timeout: 15000 });

    // NOVA notices, continues the conversation and asks for the missing details.
    await expect(host.locator('.msg.bot').last()).toContainText("Your Google Calendar is connected. What time tomorrow, and what's it about?", { timeout: 20000 });
    await expect.poll(() => consentTab.isClosed(), { timeout: 10000 }).toBe(true); // Google's tab is closed for you
    // One answer carries both the time and the topic; the length defaults to 30 minutes.
    reply = await say(host, '11 am, project discussion');
    await expect(reply).toContainText('Just to confirm, should I schedule “Project discussion” tomorrow at 11:00 AM for 30 minutes?');
    // Changing the time and length before confirming
    reply = await say(host, 'Actually make it 12 pm for one hour');
    await expect(reply).toContainText('tomorrow at 12:00 PM for one hour?');
    expect(stack.google.events).toHaveLength(0);
    await page.screenshot({ path: path.join(os.tmpdir(), 'nova-calendar-confirm.png') });

    reply = await say(host, 'haan');
    await expect(reply).toContainText('“Project discussion” is on your Google Calendar');
    expect(stack.google.events).toHaveLength(1);
    const ev = stack.google.events[0];
    expect(ev.summary).toBe('Project discussion');
    expect(['Asia/Kolkata', 'Asia/Calcutta']).toContain(ev.start.timeZone); // Chromium reports the older IANA alias
    expect(ev.start.dateTime).toMatch(/T12:00:00\+05:30$/);
    expect(ev.end.dateTime).toMatch(/T13:00:00\+05:30$/);
    await expect(host.locator('.link', { hasText: 'Open in Google Calendar' })).toBeVisible();
    await expect(host.locator('.step.ok', { hasText: 'Added to Google Calendar' })).toBeVisible();

    // Already connected: no second permission prompt, but details are still confirmed.
    reply = await say(host, 'Book a call called Design review the day after tomorrow at 4 pm for 30 minutes');
    await expect(reply).toContainText('Just to confirm');
    await expect(reply).not.toContainText('permission');
  } finally {
    await stack.stop();
  }
});

test('denying Google permission leaves Calendar disconnected and says so', async () => {
  const stack = await startStack();
  try {
    stack.consent.answer = 'deny';
    const page = await stack.context.newPage();
    await page.goto(`http://127.0.0.1:${SITE_PORT}/`);
    const host = page.locator('#nova-host');
    await host.locator('.orb').click();
    await say(host, 'Schedule a meeting tomorrow at 3 pm');
    const card = host.locator('.card').last();
    const [tab] = await Promise.all([stack.context.waitForEvent('page'), card.getByRole('button', { name: 'Connect Google Calendar' }).click()]);
    await expect(tab.locator('h1')).toHaveText('Not connected', { timeout: 15000 });
    await expect(card).toContainText('You didn’t allow access', { timeout: 15000 });
    expect(stack.google.tokenRequests).toHaveLength(0);
    expect(stack.google.events).toHaveLength(0);
  } finally {
    await stack.stop();
  }
});

test('saying “yes” to connecting opens Google’s sign-in page without a click', async () => {
  const stack = await startStack();
  try {
    const page = await stack.context.newPage();
    await page.goto(`http://127.0.0.1:${SITE_PORT}/`);
    const host = page.locator('#nova-host');
    await host.locator('.orb').click();
    await say(host, 'hey nova schedule my meeting for tommorow');
    expect(stack.consent.requests).toHaveLength(0);
    const [consentTab] = await Promise.all([stack.context.waitForEvent('page'), say(host, 'yes')]);
    await expect(consentTab.locator('h1')).toHaveText('Google Calendar connected', { timeout: 15000 });
    expect(stack.consent.requests).toHaveLength(1);
    await expect(host.locator('.msg.bot').last()).toContainText("What time tomorrow, and what's it about?", { timeout: 20000 });
    const reply = await say(host, '11 am');
    await expect(reply).toContainText('should I schedule “Meeting” tomorrow at 11:00 AM for 30 minutes?');
    await expect(host.getByRole('button', { name: 'Rename it' })).toBeVisible();
    expect(stack.google.events).toHaveLength(0);
  } finally {
    await stack.stop();
  }
});
