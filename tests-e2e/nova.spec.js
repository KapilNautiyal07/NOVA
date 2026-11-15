import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { API, SITE, askOrb, askPanel, connect, expect, launchNova, openOrb, serviceWorker, test } from './fixtures.js';

test.describe.configure({ mode: 'serial' });

const reminderAlarms = (sw) => sw.evaluate(async () => (await chrome.alarms.getAll()).filter((a) => a.name.startsWith('nova:r:')));

test('orb appears, opens, and can be dragged (position persists)', async ({ context, account, sw }) => {
  const page = await context.newPage();
  await page.goto(`${SITE}/article.html`);
  const host = await openOrb(page);
  await expect(host.locator('.root')).toHaveAttribute('data-state', 'idle');
  await page.keyboard.press('Escape');
  await expect(host.locator('.panel')).toBeHidden();

  const box = await host.locator('.orb').boundingBox();
  await page.mouse.move(box.x + 26, box.y + 26);
  await page.mouse.down();
  await page.mouse.move(box.x - 200, box.y - 150, { steps: 8 });
  await page.mouse.up();
  await expect.poll(() => sw.evaluate(async () => (await chrome.storage.local.get('settings')).settings?.orbPosition)).toBeTruthy();
  await expect(host.locator('.panel')).toBeHidden(); // a drag is not a click
});

test('TEST 1 — "Remind me to visit this website tomorrow at 10 AM" saves URL, title, reminder and schedules the notification', async ({ context, account, sw }) => {
  const page = await context.newPage();
  await page.goto(`${SITE}/article.html`);
  await openOrb(page);
  const reply = await askOrb(page, 'Remind me to visit this website tomorrow at 10 AM');
  await expect(reply).toContainText("I'll remind you tomorrow at 10:00 AM");
  await expect(page.locator('#nova-host .step', { hasText: 'Scheduled reminder' })).toBeVisible();

  const [rem] = await account.call('/api/reminders?status=pending');
  expect(rem.related_url).toBe(`${SITE}/article.html`);
  expect(rem.related_title).toBe('LangGraph Guide — Building Agents');
  const local = new Date(rem.scheduled_at).toLocaleString('en-US', { timeZone: 'Asia/Kolkata', hour: 'numeric', hour12: false });
  expect(Number(local)).toBe(10);
  const [site] = await account.call('/api/websites');
  expect(site.url).toBe(`${SITE}/article.html`);

  const alarms = await reminderAlarms(sw);
  const alarm = alarms.find((a) => a.name.startsWith(`nova:r:${rem.id}:0:`));
  expect(alarm, 'chrome.alarms entry for the reminder').toBeTruthy();
  expect(Math.abs(alarm.scheduledTime - Date.parse(rem.scheduled_at))).toBeLessThan(1000);
});

test('TEST 2 — "Set an alarm for my meeting at 3 PM" has correct time and appears in the dashboard', async ({ context, account, extId, sw }) => {
  const panel = await context.newPage();
  await panel.goto(`chrome-extension://${extId}/src/sidepanel/index.html#assistant`);
  const reply = await askPanel(panel, 'Set an alarm for my meeting at 3 PM');
  await expect(reply).toContainText('Alarm set for');
  await expect(reply).toContainText('3:00 PM');

  const [rem] = await account.call('/api/reminders?status=pending');
  const fire = new Date(rem.scheduled_at);
  const hour = Number(fire.toLocaleString('en-US', { timeZone: 'Asia/Kolkata', hour: 'numeric', hour12: false }));
  expect(hour).toBe(15);
  expect(fire.getTime()).toBeGreaterThan(Date.now());
  expect(fire.getTime() - Date.now()).toBeLessThan(24 * 3600 * 1000);
  expect(rem.kind).toBe('alarm');

  await panel.locator('.nav-item[data-view="reminders"]').click();
  const row = panel.locator('#reminder-list .item', { hasText: 'Meeting' });
  await expect(row).toBeVisible();
  await expect(row).toContainText('3:00 PM');
  await expect(row.locator('.pill.alarm')).toBeVisible();
  expect((await reminderAlarms(sw)).some((a) => a.name.startsWith(`nova:r:${rem.id}:`))).toBe(true);
});

test('TEST 3 — "Save this PDF" starts a chrome.downloads download and stores metadata', async ({ context, account, sw }) => {
  const page = await context.newPage();
  await page.goto(`${SITE}/article.html`);
  await openOrb(page);
  const reply = await askOrb(page, 'Save this PDF');
  await expect(reply).toContainText('Downloading “report.pdf”');

  const [file] = await account.call('/api/files');
  expect(file.file_name).toBe('report.pdf');
  expect(file.file_url).toBe(`${SITE}/files/report.pdf`);
  expect(file.source_page_url).toBe(`${SITE}/article.html`);

  const downloads = await sw.evaluate(async (url) => chrome.downloads.search({ url }), `${SITE}/files/report.pdf`);
  expect(downloads.length).toBe(1);
  await expect.poll(async () => (await account.call('/api/files'))[0].status, { timeout: 15000 }).toBe('complete');
  const done = (await account.call('/api/files'))[0];
  expect(done.local_download_id).toBe(downloads[0].id);
  expect(done.size_bytes).toBeGreaterThan(100);
});

test('TEST 4 — "Show all my pending tasks" returns the right records', async ({ context, account, extId }) => {
  await account.call('/api/tasks', { method: 'POST', body: JSON.stringify({ title: 'Write MCA report', priority: 'high' }) });
  await account.call('/api/tasks', { method: 'POST', body: JSON.stringify({ title: 'Call the bank' }) });
  const done = await account.call('/api/tasks', { method: 'POST', body: JSON.stringify({ title: 'Already finished' }) });
  await account.call(`/api/tasks/${done.id}/complete`, { method: 'POST' });

  const panel = await context.newPage();
  await panel.goto(`chrome-extension://${extId}/src/sidepanel/index.html#assistant`);
  const reply = await askPanel(panel, 'Show all my pending tasks');
  await expect(reply).toContainText('You have 2 pending tasks');
  await expect(reply).toContainText('Write MCA report');
  await expect(reply).toContainText('Call the bank');
  await expect(reply).not.toContainText('Already finished');

  await panel.locator('.nav-item[data-view="tasks"]').click();
  await expect(panel.locator('#task-list .item')).toHaveCount(2);
});

test('TEST 6 — "Open the website I saved yesterday" opens the right page', async ({ context, account, env, extId }) => {
  await account.call('/api/websites', { method: 'POST', body: JSON.stringify({ url: `${SITE}/other.html`, title: "Yesterday's Reading" }) });
  await account.call('/api/websites', { method: 'POST', body: JSON.stringify({ url: `${SITE}/article.html`, title: 'Today page' }) });
  const db = new DatabaseSync(env.dbPath);
  const y = new Date(Date.now() - 24 * 3600 * 1000).toISOString().replace('T', ' ').replace('Z', '');
  db.prepare('UPDATE saved_websites SET created_at = ?, last_visited_at = ? WHERE url = ?').run(y, y, `${SITE}/other.html`);
  db.close();

  const panel = await context.newPage();
  await panel.goto(`chrome-extension://${extId}/src/sidepanel/index.html#assistant`);
  const opened = context.waitForEvent('page', (p) => p.url().includes('/other.html'));
  const reply = await askPanel(panel, 'Open the website I saved yesterday');
  await expect(reply).toContainText("Opening “Yesterday's Reading”");
  const tab = await opened;
  expect(tab.url()).toBe(`${SITE}/other.html`);
});

test('TEST 7 — after a browser restart, pending reminders are restored as alarms', async ({ env }) => {
  const profile = fs.mkdtempSync(path.join(env.tmp, 'restart-'));
  let ctx = await launchNova(env, profile);
  let sw = await serviceWorker(ctx);
  const account = await connect(sw, ctx, 'restart');
  const a = await account.call('/api/reminders', { method: 'POST', body: JSON.stringify({ title: 'Restart A', scheduled_at: new Date(Date.now() + 2 * 3600e3).toISOString() }) });
  const b = await account.call('/api/reminders', { method: 'POST', body: JSON.stringify({ title: 'Restart B', scheduled_at: new Date(Date.now() + 26 * 3600e3).toISOString(), kind: 'meeting', notify_offsets: [10, 0] }) });
  await sw.evaluate(() => globalThis.nova.syncReminders());
  expect((await reminderAlarms(sw)).length).toBe(3);
  // Worst case: Chrome dropped every alarm, then the browser quits.
  await sw.evaluate(() => chrome.alarms.clearAll());
  await ctx.close();

  ctx = await launchNova(env, profile);
  sw = await serviceWorker(ctx);
  const ids = () => reminderAlarms(sw).then((al) => al.map((x) => x.name.split(':')[2]).sort());
  await expect.poll(ids, { timeout: 15000 }).toEqual([String(a.id), String(b.id), String(b.id)].sort());

  // Also restored from the local cache when the server is unreachable.
  await sw.evaluate(async () => {
    await chrome.alarms.clearAll();
    await chrome.storage.local.set({ backendUrl: 'http://127.0.0.1:9' }); // nothing listens here
    await globalThis.nova.syncReminders();
  });
  expect((await reminderAlarms(sw)).length).toBe(3);
  await ctx.close();
});

test('TEST 8 — with the microphone denied, NOVA explains and text interaction still works', async ({ context, account }) => {
  await context.clearPermissions(); // microphone not granted
  const page = await context.newPage();
  await page.goto(`${SITE}/article.html`);
  const host = await openOrb(page);
  await host.locator('.mic').click();
  const err = host.locator('.msg.bot.error');
  await expect(err).toBeVisible({ timeout: 15000 });
  await expect(err).toContainText(/type|Microphone|microphone/);
  await expect(host.locator('.root')).not.toHaveAttribute('data-state', 'listening');

  const reply = await askOrb(page, 'Remind me to stretch in 30 minutes');
  await expect(reply).toContainText("I'll remind you");
  expect((await account.call('/api/reminders?status=pending')).length).toBe(1);
});

test('page reading requires explicit permission before summarizing', async ({ context, account }) => {
  const page = await context.newPage();
  await page.goto(`${SITE}/article.html`);
  const host = await openOrb(page);
  const reply = await askOrb(page, 'Summarize this page');
  await expect(reply).toContainText("need to read this page's text");
  const card = host.locator('.card');
  await expect(card).toContainText('Allow NOVA to read');
  await card.getByText('Allow once').click();
  await expect(host.locator('.msg.bot').last()).toContainText(/LangGraph|summary/i, { timeout: 20000 });
});

test('popup shows upcoming items and options page onboarding renders', async ({ context, account, extId }) => {
  await account.call('/api/reminders', { method: 'POST', body: JSON.stringify({ title: 'Popup reminder', scheduled_at: new Date(Date.now() + 3600e3).toISOString() }) });
  const popup = await context.newPage();
  await popup.goto(`chrome-extension://${extId}/src/popup/popup.html`);
  await expect(popup.locator('#reminders')).toContainText('Popup reminder');
  const opts = await context.newPage();
  await opts.goto(`chrome-extension://${extId}/src/options/options.html`);
  await expect(opts.locator('h1')).toHaveText('Settings');
  await expect(opts.locator('text=Kapil')).toBeVisible();
});

test('onboarding: register from the options page connects the extension', async ({ context, extId, sw }) => {
  await sw.evaluate(async (api) => {
    await chrome.storage.local.clear();
    await chrome.storage.local.set({ backendUrl: api });
  }, API);
  const opts = await context.newPage();
  await opts.goto(`chrome-extension://${extId}/src/options/options.html`);
  const form = opts.locator('#register-form');
  await form.locator('input[name="backend"]').fill(API);
  await form.locator('input[name="name"]').fill('New User');
  await form.locator('input[name="email"]').fill(`new-${Date.now()}@example.com`);
  await form.getByRole('button', { name: /Create account/ }).click();
  await expect(opts.locator('.toast')).toContainText('Connected');
  expect(await sw.evaluate(async () => !!(await chrome.storage.local.get('token')).token)).toBe(true);
});

test('website memory conversation — follow-up after navigating still reminds about the original page', async ({ context, account, sw }) => {
  const page = await context.newPage();
  await page.goto(`${SITE}/article.html`);
  await openOrb(page);
  const ask = await askOrb(page, 'I want to visit this website again tomorrow.');
  await expect(ask).toContainText("I've saved this website. What time tomorrow should I remind you?");

  // The user wanders off to another page before answering.
  await page.goto(`${SITE}/other.html`);
  await openOrb(page);
  const done = await askOrb(page, '10 AM');
  await expect(done).toContainText("I'll remind you tomorrow at 10:00 AM");

  const [rem] = await account.call('/api/reminders?status=pending');
  expect(rem.related_url).toBe(`${SITE}/article.html`);
  expect(rem.title).toContain('LangGraph Guide');
  const alarms = await reminderAlarms(sw);
  expect(alarms.some((a) => a.name.startsWith(`nova:r:${rem.id}:0:`))).toBe(true);
});
