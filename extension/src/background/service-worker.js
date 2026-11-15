// NOVA background service worker (MV3).
// Responsibilities: talk to the backend, execute client actions (open tabs, downloads),
// schedule reminder alarms + notifications, and survive worker suspension / browser restarts.
import { api, ApiError, qs } from '../lib/api.js';
import { getSessionId, getState, getSettings } from '../lib/storage.js';
import * as S from '../lib/scheduler.js';
import { createVoiceLoop } from './voice-loop.js';

const K = {
  reminders: 'nova.reminders',
  delivered: 'nova.delivered',
  outbox: 'nova.outbox',
  downloads: 'nova.downloads',
  savedUrls: 'nova.savedUrls',
  suggested: 'nova.suggested',
  lastSync: 'nova.lastSync',
  visits: 'nova.visits',
};
const ICON = chrome.runtime.getURL('icons/icon128.png');
const DASHBOARD = chrome.runtime.getURL('src/sidepanel/index.html');
const inflight = new Set();

// ------------------------------------------------------------------ storage helpers
const get = async (key, fallback) => (await chrome.storage.local.get(key))[key] ?? fallback;
const set = (key, value) => chrome.storage.local.set({ [key]: value });

// ------------------------------------------------------------------ offline outbox
async function enqueue(op) {
  const box = await get(K.outbox, []);
  box.push({ ...op, at: Date.now() });
  await set(K.outbox, box.slice(-100));
}

async function flushOutbox() {
  const box = await get(K.outbox, []);
  if (!box.length) return;
  const remaining = [];
  for (const op of box) {
    try {
      await api(op.path, { method: op.method, body: op.body });
    } catch (e) {
      if (e.offline) remaining.push(op); // keep for later; drop permanent (4xx) failures
    }
  }
  await set(K.outbox, remaining);
}

async function apiOrQueue(path, method, body) {
  try {
    return await api(path, { method, body });
  } catch (e) {
    if (e.offline) await enqueue({ path, method, body });
    else throw e;
    return null;
  }
}

// ------------------------------------------------------------------ reminders + alarms
async function rebuildAlarms({ showMissed = true } = {}) {
  const reminders = await get(K.reminders, []);
  const delivered = new Set(await get(K.delivered, []));
  const now = Date.now();
  const plan = S.planAlarms(reminders, now, delivered);
  const existing = await chrome.alarms.getAll();
  const { create, clear } = S.diffAlarms(existing, plan.alarms);
  await Promise.all(clear.map((n) => chrome.alarms.clear(n)));
  for (const a of create) await chrome.alarms.create(a.name, { when: a.when });
  if (showMissed) {
    for (const m of plan.missed) {
      const late = now - S.fireTime(m.reminder) > 2 * 60000;
      await deliver(m.reminder, 0, m.name, { missed: late });
    }
  }
  await set(K.delivered, [...S.pruneDelivered(new Set(await get(K.delivered, [])), reminders, now)]);
  await updateBadge(reminders);
  return { scheduled: plan.alarms.length, missed: plan.missed.length };
}

async function syncReminders() {
  const state = await getState();
  if (!state.token) return { scheduled: 0, offline: false, connected: false };
  await flushOutbox();
  let offline = false;
  try {
    const reminders = await api(`/api/reminders${qs({ status: ['pending', 'snoozed'], limit: 500 })}`, { state });
    await set(K.reminders, reminders);
    await set(K.lastSync, Date.now());
    refreshSavedUrls(state).catch(() => {});
  } catch (e) {
    offline = !!e.offline; // keep the cached reminders and alarms
  }
  const res = await rebuildAlarms();
  return { ...res, offline, connected: true };
}

async function refreshSavedUrls(state) {
  const sites = await api('/api/websites?limit=200', { state });
  await set(K.savedUrls, sites.map((s) => s.url));
}

async function updateBadge(reminders) {
  const end = new Date();
  end.setHours(23, 59, 59, 999);
  const today = reminders.filter((r) => ['pending', 'snoozed'].includes(r.status) && S.fireTime(r) <= end.getTime()).length;
  await chrome.action.setBadgeBackgroundColor({ color: '#6E56FF' });
  await chrome.action.setBadgeText({ text: today ? String(today) : '' });
}

async function deliver(reminder, offset, name, { missed = false } = {}) {
  if (inflight.has(name)) return;
  inflight.add(name);
  try {
    const delivered = new Set(await get(K.delivered, []));
    if (delivered.has(name)) return;
    delivered.add(name);
    await set(K.delivered, [...delivered]);
    const settings = await getSettings();
    const opts = S.notificationFor(reminder, offset, { missed, requireInteraction: settings.notifications.requireInteraction });
    await chrome.notifications.create(S.notificationId(reminder.id, offset), { ...opts, iconUrl: ICON });
    if (offset === 0) {
      const updated = await apiOrQueue(`/api/reminders/${reminder.id}/fired`, 'POST');
      const cache = await get(K.reminders, []);
      const next = updated && ['pending', 'snoozed'].includes(updated.status)
        ? cache.map((r) => (r.id === reminder.id ? updated : r))
        : cache.filter((r) => r.id !== reminder.id || r.recurrence);
      await set(K.reminders, next);
      if (updated?.status === 'pending') await rebuildAlarms({ showMissed: false });
      else await updateBadge(next);
    }
  } finally {
    inflight.delete(name);
  }
}

async function reminderById(id) {
  let r = (await get(K.reminders, [])).find((x) => x.id === id);
  if (!r) {
    try {
      r = (await api(`/api/reminders${qs({ limit: 500 })}`)).find((x) => x.id === id);
    } catch {
      /* offline */
    }
  }
  return r;
}

async function snooze(id, minutes = 10) {
  const reminders = S.localSnooze(await get(K.reminders, []), id, minutes, Date.now());
  await set(K.reminders, reminders);
  await rebuildAlarms({ showMissed: false });
  const updated = await apiOrQueue(`/api/reminders/${id}/snooze`, 'POST', { minutes });
  if (updated) {
    await set(K.reminders, (await get(K.reminders, [])).map((r) => (r.id === id ? updated : r)));
    await rebuildAlarms({ showMissed: false });
  }
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === S.SYNC_ALARM) return void syncReminders();
  const p = S.parseAlarmName(alarm.name);
  if (!p) return;
  const reminder = await reminderById(p.id);
  if (!reminder || !['pending', 'snoozed'].includes(reminder.status)) return;
  if (S.fireTime(reminder) !== p.fireAt) return void rebuildAlarms(); // rescheduled meanwhile
  await deliver(reminder, p.offset, alarm.name);
});

chrome.notifications.onButtonClicked.addListener(async (notifId, index) => {
  const p = S.parseNotificationId(notifId);
  if (!p) return;
  chrome.notifications.clear(notifId);
  const reminder = await reminderById(p.id);
  if (index === 1) return void snooze(p.id, 10);
  if (reminder?.related_url) return void openUrl(reminder.related_url);
  await apiOrQueue(`/api/reminders/${p.id}/complete`, 'POST');
  await set(K.reminders, (await get(K.reminders, [])).filter((r) => r.id !== p.id || r.recurrence));
  await syncReminders();
});

chrome.notifications.onClicked.addListener(async (notifId) => {
  const p = S.parseNotificationId(notifId);
  chrome.notifications.clear(notifId);
  if (!p) return;
  const reminder = await reminderById(p.id);
  if (reminder?.related_url) openUrl(reminder.related_url);
});

// ------------------------------------------------------------------ actions
function safeUrl(url) {
  try {
    const u = new URL(url);
    return ['http:', 'https:'].includes(u.protocol) ? u.href : null;
  } catch {
    return null;
  }
}

async function openUrl(url) {
  const safe = safeUrl(url);
  if (!safe) return { ok: false, error: 'blocked non-http URL' };
  const tab = await chrome.tabs.create({ url: safe, active: true });
  return { ok: true, tabId: tab.id };
}

function safeFilename(name) {
  return (name || 'download')
    .split('/')
    .map((part) => part.replace(/[<>:"\\|?*\x00-\x1f]/g, '_').replace(/^\.+/, '').trim())
    .filter((part) => part && part !== '..')
    .join('/')
    .slice(0, 240) || 'download';
}

async function startDownload({ file_id, url, filename }) {
  const safe = safeUrl(url);
  if (!safe) return { ok: false, error: 'blocked non-http URL' };
  try {
    const downloadId = await chrome.downloads.download({ url: safe, filename: safeFilename(filename), conflictAction: 'uniquify', saveAs: false });
    const map = await get(K.downloads, {});
    map[downloadId] = file_id;
    await set(K.downloads, map);
    await apiOrQueue(`/api/files/${file_id}/status`, 'PATCH', { status: 'in_progress', local_download_id: downloadId });
    return { ok: true, downloadId };
  } catch (e) {
    await apiOrQueue(`/api/files/${file_id}/status`, 'PATCH', { status: 'failed', error: String(e.message || e).slice(0, 200) });
    return { ok: false, error: String(e.message || e) };
  }
}

chrome.downloads.onChanged.addListener(async (delta) => {
  if (!delta.state) return;
  const map = await get(K.downloads, {});
  const fileId = map[delta.id];
  if (!fileId) return;
  const [item] = await chrome.downloads.search({ id: delta.id });
  if (delta.state.current === 'complete') {
    await apiOrQueue(`/api/files/${fileId}/status`, 'PATCH', {
      status: 'complete', local_download_id: delta.id, local_path: item?.filename?.slice(0, 1000), size_bytes: item?.fileSize || item?.totalBytes || undefined,
    });
  } else if (delta.state.current === 'interrupted') {
    await apiOrQueue(`/api/files/${fileId}/status`, 'PATCH', { status: 'interrupted', error: (item?.error || 'interrupted').slice(0, 200) });
  }
});

async function downloadProgress() {
  const map = await get(K.downloads, {});
  const ids = Object.keys(map).map(Number).slice(-20);
  const items = [];
  for (const id of ids) {
    const [it] = await chrome.downloads.search({ id });
    if (it) items.push({ downloadId: id, fileId: map[id], state: it.state, received: it.bytesReceived, total: it.totalBytes, filename: it.filename, error: it.error });
  }
  return items;
}

async function executeActions(actions = [], ctx = {}) {
  const results = [];
  for (const a of actions) {
    try {
      if (a.type === 'open_url') results.push({ type: a.type, ...(await openUrl(a.payload.url)) });
      else if (a.type === 'download') results.push({ type: a.type, ...(await startDownload(a.payload)) });
      else if (a.type === 'sync_reminders') results.push({ type: a.type, ok: true, ...(await syncReminders()) });
      else results.push({ type: a.type, ok: true, deferred: true }); // handled by the UI (show_view, request_page_content)
    } catch (e) {
      results.push({ type: a.type, ok: false, error: String(e.message || e) });
    }
  }
  return results;
}

// ------------------------------------------------------------------ assistant
function cleanPage(page) {
  if (!page || !page.url || S.isRestrictedUrl(page.url)) return page?.url ? { restricted: true } : null;
  const out = {
    url: page.url,
    title: (page.title || '').slice(0, 500),
    content_type: page.content_type || null,
    selection: page.selection ? page.selection.slice(0, 4000) : null,
    description: page.description ? page.description.slice(0, 500) : null,
    file_links: (page.file_links || []).filter((l) => safeUrl(l.url)).slice(0, 25),
  };
  if (page.text) out.text = page.text.slice(0, 20000);
  return out;
}

async function ask({ message, page, pageTextAllowed = false, channel = 'text' }) {
  const body = {
    session_id: await getSessionId(),
    message: String(message || '').slice(0, 2000),
    page: cleanPage(page),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    page_text_allowed: !!(pageTextAllowed && page?.text),
    channel: channel === 'voice' ? 'voice' : 'text',
  };
  const res = await api('/api/assistant/message', { method: 'POST', body, timeoutMs: 60000 });
  res.actionResults = await executeActions(res.client_actions);
  return res;
}

// Find the web tab the user is "on" (side panel/popup pages are not web pages).
async function contextTab() {
  const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (active && /^https?:/.test(active.url || '')) return active;
  const { lastWebTab } = await chrome.storage.session.get('lastWebTab');
  if (lastWebTab) {
    try {
      return await chrome.tabs.get(lastWebTab);
    } catch {
      /* closed */
    }
  }
  const recent = (await chrome.tabs.query({ lastFocusedWindow: true }))
    .filter((t) => /^https?:/.test(t.url || ''))
    .sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0))[0];
  return recent || active || null;
}

async function sendToTab(tabId, msg, inject = true) {
  try {
    return await chrome.tabs.sendMessage(tabId, msg);
  } catch (e) {
    if (!inject) throw e;
    // Page was open before NOVA was installed/reloaded → inject the content script once.
    await chrome.scripting.executeScript({ target: { tabId }, files: ['src/content/orb.js'] });
    return chrome.tabs.sendMessage(tabId, msg);
  }
}

async function pageContext({ withText = false, tabId } = {}) {
  let tab = null;
  if (tabId !== undefined) tab = await chrome.tabs.get(tabId).catch(() => null);
  tab ||= await contextTab();
  if (!tab) return { page: null };
  const base = { url: tab.url, title: tab.title };
  // tab.url is hidden (undefined) unless NOVA has host access to that page; the orb script on the
  // page can still report it, so only a *known* restricted URL short-circuits here.
  if (tab.url && S.isRestrictedUrl(tab.url)) return { page: { ...base, restricted: true }, tabId: tab.id };
  try {
    const ctx = await sendToTab(tab.id, { type: 'nova:collect', withText });
    return { page: { ...base, ...ctx }, tabId: tab.id };
  } catch {
    // e.g. Chrome's built-in PDF viewer, where content scripts can't run
    return { page: { ...base, content_type: /\.pdf($|\?)/i.test(tab.url) ? 'application/pdf' : null, text_unavailable: true }, tabId: tab.id };
  }
}

async function suggest(page) {
  const settings = await getSettings();
  if (!settings.suggestions || !settings.orbEnabled || !page?.url) return null;
  const host = new URL(page.url).hostname;
  if (settings.disabledSites.includes(host)) return null;
  const res = await api('/api/assistant/suggest', { method: 'POST', body: { page: cleanPage({ ...page, text: null }) } });
  if (!res.message) return null;
  const key = `${host}|${res.page_type}`;
  const seen = await get(K.suggested, {});
  const today = new Date().toDateString();
  if (seen[key] === today) return null; // at most once per site & page type per day
  seen[key] = today;
  const entries = Object.entries(seen).slice(-300);
  await set(K.suggested, Object.fromEntries(entries));
  return res;
}

async function trackVisit({ url, title }) {
  const saved = await get(K.savedUrls, []);
  if (!saved.includes(url)) return { tracked: false };
  const visits = await get(K.visits, {});
  if (visits[url] && Date.now() - visits[url] < 30 * 60000) return { tracked: false };
  visits[url] = Date.now();
  await set(K.visits, Object.fromEntries(Object.entries(visits).slice(-200)));
  return api('/api/websites/visit', { method: 'POST', body: { url, title } });
}

async function transcribe({ audio, mime, preview = false }) {
  const bin = Uint8Array.from(atob(audio), (c) => c.charCodeAt(0));
  const form = new FormData();
  const ext = mime.includes('wav') ? 'wav' : mime.includes('ogg') ? 'ogg' : 'webm';
  form.append('audio', new Blob([bin], { type: mime }), `speech.${ext}`);
  if (preview) form.append('preview', 'true');
  return api('/api/voice/transcribe', { method: 'POST', form, timeoutMs: 45000 });
}

// Natural voice (Gemini TTS on the server). Returns { audio: base64 WAV, spoken_text }.
async function synthesize({ text, voice }) {
  return api('/api/voice/speak', { method: 'POST', body: { text: String(text).slice(0, 4000), voice: voice || undefined }, timeoutMs: 30000 });
}

// ------------------------------------------------------------------ Google Calendar
// OAuth runs entirely between Google and the NOVA server; the extension only opens Google's
// consent page in a tab (after the user clicked "Connect") and then checks the outcome.
async function calendarConnect() {
  const { auth_url, attempt_id } = await api('/api/integrations/google/connect', { method: 'POST' });
  // Only ever open Google's real consent page (or a loopback stand-in used for local testing).
  const u = new URL(auth_url);
  const google = u.protocol === 'https:' && u.hostname === 'accounts.google.com';
  const loopback = ['127.0.0.1', 'localhost'].includes(u.hostname);
  if (!google && !loopback) throw new Error('Unexpected sign-in address from the server.');
  const tab = await chrome.tabs.create({ url: auth_url, active: true });
  await chrome.storage.session.set({ calendarAuthTab: tab.id });
  return { attempt_id };
}

async function calendarStatus({ attempt } = {}) {
  const st = await api(`/api/integrations/google/status${attempt ? `?attempt=${encodeURIComponent(attempt)}` : ''}`);
  const outcome = st.attempt?.outcome;
  if (outcome === 'connected') {
    // Close Google's tab once it has shown "connected", and bring the user back.
    const { calendarAuthTab } = await chrome.storage.session.get('calendarAuthTab');
    if (calendarAuthTab) setTimeout(() => chrome.tabs.remove(calendarAuthTab).catch(() => {}), 1500);
    await chrome.storage.session.remove('calendarAuthTab');
  }
  return st;
}

async function openDashboard(windowId, view) {
  if (view) await chrome.storage.session.set({ dashboardView: view });
  try {
    if (windowId === undefined) windowId = (await chrome.windows.getLastFocused()).id;
    await chrome.sidePanel.open({ windowId });
    return { ok: true, mode: 'sidepanel' };
  } catch {
    await chrome.tabs.create({ url: DASHBOARD + (view ? `#${view}` : '') });
    return { ok: true, mode: 'tab' };
  }
}

const voiceLoop = createVoiceLoop({
  ask,
  pageContext,
  transcribe,
  synthesize,
  sendToTab,
  refreshBadge: async () => updateBadge(await get(K.reminders, [])),
});

const HANDLERS = {
  ...voiceLoop.handlers,
  'nova:ask': (m) => ask(m),
  'nova:context': (m) => pageContext(m),
  'nova:suggest': (m) => suggest(m.page),
  'nova:visit': (m) => trackVisit(m),
  'nova:transcribe': (m) => transcribe(m),
  'nova:calendarConnect': () => calendarConnect(),
  'nova:calendarStatus': (m) => calendarStatus(m),
  'nova:calendarDisconnect': () => api('/api/integrations/google', { method: 'DELETE' }),
  'nova:voiceConfig': () => api('/api/voice/config'),
  'nova:openMicGrant': () => chrome.tabs.create({ url: chrome.runtime.getURL('src/permission/mic.html') }),
  'nova:sync': () => syncReminders(),
  'nova:rebuild': () => rebuildAlarms({ showMissed: false }),
  'nova:downloads': () => downloadProgress(),
  'nova:download': async ({ file }) => {
    const res = await startDownload({ file_id: file.id, url: file.file_url, filename: `NOVA/${file.folder ? `${file.folder}/` : ''}${file.file_name}` });
    if (!res.ok) throw new Error(res.error);
    return res;
  },
  'nova:snooze': (m) => snooze(m.id, m.minutes),
  'nova:openDashboard': (m, sender) => openDashboard(sender.tab?.windowId, m.view),
  'nova:openOptions': () => chrome.runtime.openOptionsPage(),
  'nova:openUrl': (m) => openUrl(m.url),
  'nova:alarms': async () => (await chrome.alarms.getAll()).filter((a) => a.name.startsWith(S.ALARM_PREFIX)),
  'nova:testNotification': async () => {
    await chrome.notifications.create(`nova-test:${Date.now()}`, { type: 'basic', iconUrl: ICON, title: '🔔 NOVA', message: 'Notifications are working.', priority: 1 });
    return { ok: true };
  },
};

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const handler = HANDLERS[msg?.type];
  if (!handler) return false;
  Promise.resolve()
    .then(() => handler(msg, sender))
    .then((data) => sendResponse({ ok: true, data }))
    .catch((e) => sendResponse({ ok: false, error: e.message || String(e), status: e instanceof ApiError ? e.status : 0, offline: !!e.offline }));
  return true; // async response
});

// ------------------------------------------------------------------ commands, tabs, lifecycle
chrome.commands.onCommand.addListener(async (command, tab) => {
  tab = tab || (await chrome.tabs.query({ active: true, lastFocusedWindow: true }))[0];
  if (command === 'open-dashboard') return void openDashboard(tab?.windowId);
  if (command === 'toggle-assistant') {
    if (tab && !S.isRestrictedUrl(tab.url)) {
      try {
        return void (await sendToTab(tab.id, { type: 'nova:activate', listen: true }));
      } catch {
        /* fall through to side panel */
      }
    }
    openDashboard(tab?.windowId, 'assistant');
  }
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const tab = await chrome.tabs.get(tabId);
    if (/^https?:/.test(tab.url || '')) await chrome.storage.session.set({ lastWebTab: tabId });
  } catch {
    /* ignore */
  }
});

async function ensureSyncAlarm() {
  if (!(await chrome.alarms.get(S.SYNC_ALARM))) await chrome.alarms.create(S.SYNC_ALARM, { periodInMinutes: 15, delayInMinutes: 1 });
}

chrome.runtime.onInstalled.addListener(async ({ reason }) => {
  await ensureSyncAlarm();
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false }).catch(() => {});
  if (reason === 'install') chrome.runtime.openOptionsPage();
  await syncReminders();
});

chrome.runtime.onStartup.addListener(async () => {
  await ensureSyncAlarm();
  await syncReminders();
  await voiceLoop.sync().catch(() => {});
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return;
  if (changes.token?.newValue) syncReminders();
  if (changes.token || changes.settings) voiceLoop.sync().catch(() => {});
});

// Every time the worker wakes up (it may have been suspended), make sure alarms match the cache
// and the "Hey NOVA" listener is running if it's turned on.
ensureSyncAlarm().then(() => rebuildAlarms({ showMissed: true })).catch(() => {});
voiceLoop.sync().catch(() => {});

// Exposed for diagnostics / e2e tests via the service-worker console.
globalThis.nova = { syncReminders, rebuildAlarms, pageContext, executeActions, downloadProgress, voiceLoop, K };
