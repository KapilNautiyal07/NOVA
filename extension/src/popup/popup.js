import { api, qs } from '../lib/api.js';
import { getState } from '../lib/storage.js';

const $ = (s) => document.querySelector(s);
const bg = async (msg) => {
  const r = await chrome.runtime.sendMessage(msg);
  if (!r?.ok) throw new Error(r?.error || 'Something went wrong');
  return r.data;
};

function when(iso) {
  const d = new Date(iso);
  const today = new Date().toDateString() === d.toDateString();
  const t = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return today ? `Today ${t}` : `${d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })} ${t}`;
}

function row(title, meta) {
  const div = document.createElement('div');
  div.className = 'item';
  const g = document.createElement('div');
  g.className = 'grow';
  const t = document.createElement('div');
  t.className = 'title ellipsis';
  t.textContent = title;
  const m = document.createElement('div');
  m.className = 'meta';
  m.textContent = meta;
  g.append(t, m);
  div.append(g);
  return div;
}

function fill(el, items, emptyText) {
  el.replaceChildren(...(items.length ? items : [Object.assign(document.createElement('div'), { className: 'empty-state', textContent: emptyText })]));
}

function showReply(text, error = false) {
  const r = $('#reply');
  r.textContent = text;
  r.classList.remove('hidden');
  r.classList.toggle('error', error);
}

async function load() {
  const { settings, token } = await getState();
  const t = settings.theme;
  document.documentElement.dataset.theme = t === 'auto' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : t;
  if (!token) {
    fill($('#reminders'), [], 'Not connected yet.');
    fill($('#tasks'), [], 'Open settings to connect NOVA.');
    $('#dashboard').textContent = 'Connect NOVA';
    $('#dashboard').onclick = () => chrome.runtime.openOptionsPage();
    return;
  }
  try {
    const [rems, tasks] = await Promise.all([api(`/api/reminders${qs({ status: ['pending', 'snoozed'], limit: 20 })}`), api('/api/tasks?due=today')]);
    rems.sort((a, b) => new Date(a.next_fire_at) - new Date(b.next_fire_at));
    fill($('#reminders'), rems.slice(0, 3).map((r) => row(r.title, `${when(r.next_fire_at)}${r.kind !== 'reminder' ? ` · ${r.kind}` : ''}`)), 'No upcoming reminders.');
    fill($('#tasks'), tasks.slice(0, 4).map((x) => row(x.title, `${x.priority} priority`)), 'Nothing due today 🎉');
  } catch (e) {
    fill($('#reminders'), [], e.message);
    fill($('#tasks'), [], '');
  }
}

$('#ask').addEventListener('submit', async (e) => {
  e.preventDefault();
  const message = $('#q').value.trim();
  if (!message) return;
  showReply('Thinking…');
  try {
    const { page } = await bg({ type: 'nova:context' });
    const r = await bg({ type: 'nova:ask', message, page });
    showReply(r.reply, r.status === 'error');
    $('#q').value = '';
    load();
  } catch (err) {
    showReply(err.message, true);
  }
});

$('#dashboard').addEventListener('click', async () => {
  const win = await chrome.windows.getCurrent();
  try {
    await chrome.sidePanel.open({ windowId: win.id }); // popup click is a user gesture
  } catch {
    await chrome.tabs.create({ url: chrome.runtime.getURL('src/sidepanel/index.html') });
  }
  window.close();
});

$('#save').addEventListener('click', async () => {
  try {
    const { page } = await bg({ type: 'nova:context' });
    const r = await bg({ type: 'nova:ask', message: 'Remember this website', page });
    showReply(r.reply, r.status === 'error');
  } catch (err) {
    showReply(err.message, true);
  }
});

$('#settings').addEventListener('click', () => chrome.runtime.openOptionsPage());
load();
