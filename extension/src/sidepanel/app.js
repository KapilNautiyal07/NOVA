// NOVA dashboard — side panel (and options page when NOVA_MODE === 'options').
import { api, qs } from '../lib/api.js';
import { getState, newSession, updateSettings } from '../lib/storage.js';
import { expectsAnswer } from '../lib/wake.js';

const MODE = globalThis.NOVA_MODE || 'panel';
const $ = (s, r = document) => r.querySelector(s);

// ---------------------------------------------------------------- tiny DOM builder
function h(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (k === 'html') n.innerHTML = v; // only used with static icon markup
    else if (k in n && typeof v !== 'string') n[k] = v;
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return n;
}

const ICON = {
  overview: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
  assistant: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8"/><path d="M12 8v8M8 12h8"/></svg>',
  reminders: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>',
  tasks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>',
  memory: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>',
  files: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/></svg>',
  settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
  mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>',
  send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4z"/></svg>',
  open: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 3h7v7M10 14L21 3M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/></svg>',
  snooze: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2 2M5 3L2 6M22 6l-3-3"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M5 12l5 5L20 7"/></svg>',
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5"/></svg>',
};
const iconBtn = (name, title, onclick, cls = '') => h('button', { class: `icon-btn ${cls}`, title, 'aria-label': title, onclick, html: ICON[name] });

// ---------------------------------------------------------------- state
const S = {
  view: 'overview', user: null, settings: null, token: null, backendUrl: '', online: null,
  reminders: [], tasks: [], websites: [], files: [], chat: [], chatLoaded: false,
  filters: { reminders: 'upcoming', tasks: { status: 'open', priority: '', due: '' }, memory: { q: '', period: '', category: '' }, files: { q: '' } },
};

const bg = async (msg) => {
  const r = await chrome.runtime.sendMessage(msg);
  if (!r?.ok) throw Object.assign(new Error(r?.error || 'Something went wrong'), r || {});
  return r.data;
};

function toast(text, err = false) {
  const t = h('div', { class: `toast${err ? ' err' : ''}` }, text);
  $('#toasts').append(t);
  setTimeout(() => t.remove(), 3500);
}

async function guard(fn, okMsg) {
  try {
    const out = await fn();
    if (okMsg) toast(okMsg);
    return out;
  } catch (e) {
    toast(e.message || String(e), true);
    return undefined;
  }
}

// ---------------------------------------------------------------- formatting
const DAY = 86400000;
function fmtWhen(iso, { dateOnly = false } = {}) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const t = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const diffDays = Math.floor((new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() - startToday) / DAY);
  let day;
  if (diffDays === 0) day = 'Today';
  else if (diffDays === 1) day = 'Tomorrow';
  else if (diffDays === -1) day = 'Yesterday';
  else if (diffDays > 1 && diffDays < 7) day = d.toLocaleDateString([], { weekday: 'long' });
  else day = d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' });
  return dateOnly ? day : `${day} · ${t}`;
}
const toLocalInput = (iso) => {
  if (!iso) return '';
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const fromLocalInput = (v) => (v ? new Date(v).toISOString() : null);
const fmtBytes = (n) => (!n ? '' : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const isToday = (iso) => iso && new Date(iso).toDateString() === new Date().toDateString();
const isOpen = (t) => t.status === 'pending' || t.status === 'in_progress';

// ---------------------------------------------------------------- data
async function loadAll() {
  if (!S.token) return;
  try {
    const [reminders, tasks, websites, files] = await Promise.all([
      api(`/api/reminders${qs({ limit: 300 })}`),
      api('/api/tasks'),
      api('/api/websites?limit=200'),
      api('/api/files'),
    ]);
    Object.assign(S, { reminders, tasks, websites, files, online: true });
  } catch (e) {
    S.online = !e.offline && e.status !== 401 ? S.online : false;
    if (e.status === 401) toast('Your NOVA token is no longer valid. Reconnect in Settings.', true);
  }
  renderNav();
}

async function refresh() {
  await loadAll();
  render();
}

// ---------------------------------------------------------------- shell
const VIEWS = [
  ['overview', 'Overview'], ['assistant', 'Assistant'], ['reminders', 'Reminders'], ['tasks', 'Tasks'],
  ['memory', 'Website Memory'], ['files', 'Files'], ['settings', 'Settings'],
];

function renderNav() {
  const counts = {
    reminders: S.reminders.filter((r) => ['pending', 'snoozed'].includes(r.status)).length,
    tasks: S.tasks.filter(isOpen).length,
  };
  $('#nav').replaceChildren(
    ...VIEWS.map(([id, label]) =>
      h('button', { class: `nav-item${S.view === id ? ' active' : ''}`, 'data-view': id, onclick: () => navigate(id), title: label },
        h('span', { html: ICON[id], class: 'row' }), h('span', { class: 'lbl' }, label), counts[id] ? h('span', { class: 'count' }, counts[id]) : null),
    ),
  );
  const conn = $('#conn');
  conn.className = `conn ${S.online ? 'ok' : S.online === false ? 'off' : ''}`;
  conn.title = S.online ? `Connected to ${S.backendUrl}` : S.token ? 'Server unreachable' : 'Not connected';
}

function navigate(view) {
  S.view = view;
  location.hash = view;
  renderNav();
  render(true);
  $('#main').focus({ preventScroll: true });
}

function render(force = false) {
  // The assistant view is long-lived: background data refreshes must not wipe the chat or a draft.
  if (!force && S.view === 'assistant' && $('#chat-log')) return;
  const draft = $('#chat-input')?.value;
  const fn = { overview, assistant, reminders, tasks, memory, files, settings }[S.view] || overview;
  const view = fn();
  view.classList.add('view');
  $('#main').replaceChildren(view);
  if (S.view === 'assistant') {
    const input = $('#chat-input');
    if (input && draft) input.value = draft;
    input?.focus();
  }
}

function applyTheme() {
  const t = S.settings?.theme || 'auto';
  const dark = t === 'dark' || (t === 'auto' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
}

function notConnectedBanner() {
  if (S.token) return S.online === false ? h('div', { class: 'banner err' }, h('div', { class: 'grow' }, h('b', {}, 'Server unreachable. '), `NOVA can't reach ${S.backendUrl}. Reminders already scheduled will still fire from the local cache.`), h('button', { class: 'btn sm', onclick: refresh }, 'Retry')) : null;
  return h('div', { class: 'banner warn' }, h('div', { class: 'grow' }, h('b', {}, 'Connect NOVA. '), 'Create an account on your NOVA server to start saving reminders, tasks and websites.'), h('button', { class: 'btn sm primary', onclick: () => navigate('settings') }, 'Connect'));
}

// ---------------------------------------------------------------- shared rows
function reminderRow(r) {
  const active = ['pending', 'snoozed'].includes(r.status);
  const overdue = active && new Date(r.next_fire_at) < new Date();
  return h('div', { class: 'item', 'data-id': r.id },
    h('div', { class: 'grow' },
      h('div', { class: 'title ellipsis' }, r.title),
      h('div', { class: 'meta' },
        h('span', {}, fmtWhen(r.next_fire_at)),
        r.kind !== 'reminder' ? h('span', { class: `pill ${r.kind}` }, r.kind) : null,
        r.status !== 'pending' ? h('span', { class: `pill ${r.status}` }, r.status) : null,
        overdue ? h('span', { class: 'pill overdue' }, 'overdue') : null,
        r.recurrence ? h('span', { class: 'pill accent' }, `↻ ${r.recurrence}`) : null,
        r.notify_offsets?.[0] > 0 ? h('span', {}, `${r.notify_offsets[0]}m heads-up`) : null,
        r.related_url ? h('a', { href: r.related_url, target: '_blank', class: 'ellipsis', title: r.related_url }, r.related_title || new URL(r.related_url).hostname) : null,
      ),
    ),
    h('div', { class: 'actions' },
      active ? iconBtn('snooze', 'Snooze 10 minutes', () => guard(async () => { await api(`/api/reminders/${r.id}/snooze`, { method: 'POST', body: { minutes: 10 } }); await afterReminderChange(); }, 'Snoozed for 10 minutes')) : null,
      active ? iconBtn('check', 'Mark done', () => guard(async () => { await api(`/api/reminders/${r.id}/complete`, { method: 'POST' }); await afterReminderChange(); }, 'Reminder completed')) : null,
      iconBtn('edit', 'Edit', (e) => editReminder(r, e.currentTarget.closest('.item'))),
      iconBtn('trash', 'Delete', () => confirm(`Delete reminder “${r.title}”?`) && guard(async () => { await api(`/api/reminders/${r.id}`, { method: 'DELETE' }); await afterReminderChange(); }, 'Reminder deleted')),
    ),
  );
}

async function afterReminderChange() {
  await bg({ type: 'nova:sync' }).catch(() => {});
  await refresh();
}

function editReminder(r, row) {
  const form = h('form', { class: 'inline', style: 'width:100%' },
    h('input', { class: 'input', name: 'title', value: r.title, required: true, maxlength: 300 }),
    h('div', { class: 'form-grid' },
      h('input', { class: 'input', type: 'datetime-local', name: 'when', value: toLocalInput(r.scheduled_at), required: true }),
      recurrenceSelect(r.recurrence),
    ),
    h('div', { class: 'row' }, h('button', { class: 'btn primary sm', type: 'submit' }, 'Save'), h('button', { class: 'btn sm', type: 'button', onclick: render }, 'Cancel')),
  );
  form.onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(form);
    guard(async () => {
      await api(`/api/reminders/${r.id}`, { method: 'PATCH', body: { title: f.get('title'), scheduled_at: fromLocalInput(f.get('when')), recurrence: f.get('recurrence') || null } });
      await afterReminderChange();
    }, 'Reminder updated');
  };
  row.replaceChildren(form);
}

function recurrenceSelect(value) {
  return h('select', { class: 'input', name: 'recurrence' },
    ...[['', 'Does not repeat'], ['daily', 'Every day'], ['weekdays', 'Every weekday'], ['weekly', 'Every week'], ['monthly', 'Every month']].map(([v, l]) => h('option', { value: v, selected: (value || '') === v }, l)));
}

// A date-only task ("due Friday") is stored at a default hour; it's only overdue once that day ends.
function isOverdue(t) {
  if (!isOpen(t) || !t.due_at) return false;
  const due = new Date(t.due_at);
  if (!t.due_has_time) due.setHours(23, 59, 59, 999);
  return due < new Date();
}

function taskRow(t) {
  const overdue = isOverdue(t);
  return h('div', { class: 'item', 'data-id': t.id },
    h('input', { type: 'checkbox', class: 'check', checked: t.status === 'completed', 'aria-label': `Complete ${t.title}`, onchange: (e) => guard(async () => {
      if (e.target.checked) await api(`/api/tasks/${t.id}/complete`, { method: 'POST' });
      else await api(`/api/tasks/${t.id}`, { method: 'PATCH', body: { status: 'pending' } });
      await bg({ type: 'nova:sync' }).catch(() => {});
      await refresh();
    }, e.target.checked ? 'Task completed 🎉' : 'Task reopened') }),
    h('div', { class: 'grow' },
      h('div', { class: 'title', style: t.status === 'completed' ? 'text-decoration:line-through;opacity:.6' : '' }, t.title),
      h('div', { class: 'meta' },
        t.priority !== 'medium' ? h('span', { class: `pill ${t.priority}` }, t.priority) : null,
        t.due_at ? h('span', {}, `Due ${fmtWhen(t.due_at, { dateOnly: !t.due_has_time })}`) : null,
        overdue ? h('span', { class: 'pill overdue' }, 'overdue') : null,
        t.recurrence ? h('span', { class: 'pill accent' }, `↻ ${t.recurrence}`) : null,
        t.related_url ? h('a', { href: t.related_url, target: '_blank', class: 'ellipsis' }, t.related_title || 'related page') : null,
        t.description ? h('span', { class: 'ellipsis' }, t.description) : null,
      ),
    ),
    h('div', { class: 'actions' },
      iconBtn('edit', 'Edit', (e) => editTask(t, e.currentTarget.closest('.item'))),
      iconBtn('trash', 'Delete', () => confirm(`Delete task “${t.title}”?`) && guard(async () => { await api(`/api/tasks/${t.id}`, { method: 'DELETE' }); await refresh(); }, 'Task deleted')),
    ),
  );
}

function prioritySelect(value = 'medium') {
  return h('select', { class: 'input', name: 'priority' }, ...['low', 'medium', 'high', 'urgent'].map((p) => h('option', { value: p, selected: p === value }, `${p[0].toUpperCase()}${p.slice(1)} priority`)));
}

function editTask(t, row) {
  const form = h('form', { class: 'inline', style: 'width:100%' },
    h('input', { class: 'input', name: 'title', value: t.title, required: true, maxlength: 300 }),
    h('textarea', { class: 'input', name: 'description', placeholder: 'Description (optional)', maxlength: 5000 }, t.description || ''),
    h('div', { class: 'form-grid' }, h('input', { class: 'input', type: 'datetime-local', name: 'due', value: toLocalInput(t.due_at) }), prioritySelect(t.priority)),
    h('div', { class: 'row' }, h('button', { class: 'btn primary sm', type: 'submit' }, 'Save'), h('button', { class: 'btn sm', type: 'button', onclick: render }, 'Cancel')),
  );
  form.onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(form);
    guard(async () => {
      await api(`/api/tasks/${t.id}`, { method: 'PATCH', body: { title: f.get('title'), description: f.get('description') || null, priority: f.get('priority'), due_at: fromLocalInput(f.get('due')), due_has_time: !!f.get('due') } });
      await refresh();
    }, 'Task updated');
  };
  row.replaceChildren(form);
}

function siteRow(s) {
  return h('div', { class: 'item', 'data-id': s.id },
    h('img', { src: `https://www.google.com/s2/favicons?domain=${encodeURIComponent(s.domain)}&sz=32`, width: 16, height: 16, style: 'margin-top:3px;border-radius:3px', alt: '', referrerpolicy: 'no-referrer', onerror: (e) => (e.target.style.visibility = 'hidden') }),
    h('div', { class: 'grow' },
      h('div', { class: 'title ellipsis' }, h('a', { href: s.url, target: '_blank', style: 'color:inherit' }, s.title)),
      h('div', { class: 'meta' }, h('span', {}, s.domain), s.category ? h('span', { class: 'pill' }, s.category) : null, h('span', {}, `Saved ${fmtWhen(s.created_at, { dateOnly: true })}`), s.visit_count > 1 ? h('span', {}, `${s.visit_count} visits`) : null),
      s.notes ? h('div', { class: 'small', style: 'margin-top:4px;color:var(--text-2);white-space:pre-wrap' }, s.notes.slice(0, 300)) : null,
    ),
    h('div', { class: 'actions' },
      iconBtn('open', 'Open', () => bg({ type: 'nova:openUrl', url: s.url })),
      iconBtn('edit', 'Edit notes', (e) => editSite(s, e.currentTarget.closest('.item'))),
      iconBtn('trash', 'Remove', () => confirm(`Remove “${s.title}” from memory?`) && guard(async () => { await api(`/api/websites/${s.id}`, { method: 'DELETE' }); await refresh(); }, 'Removed from memory')),
    ),
  );
}

function editSite(s, row) {
  const form = h('form', { class: 'inline', style: 'width:100%' },
    h('input', { class: 'input', name: 'title', value: s.title, maxlength: 500 }),
    h('textarea', { class: 'input', name: 'notes', placeholder: 'Notes', maxlength: 5000 }, s.notes || ''),
    h('input', { class: 'input', name: 'category', value: s.category || '', placeholder: 'Category', maxlength: 60 }),
    h('div', { class: 'row' }, h('button', { class: 'btn primary sm', type: 'submit' }, 'Save'), h('button', { class: 'btn sm', type: 'button', onclick: render }, 'Cancel')),
  );
  form.onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(form);
    guard(async () => { await api(`/api/websites/${s.id}`, { method: 'PATCH', body: { title: f.get('title'), notes: f.get('notes'), category: f.get('category') || null } }); await refresh(); }, 'Saved');
  };
  row.replaceChildren(form);
}

function fileRow(f, progress) {
  const p = progress?.find((x) => x.fileId === f.id);
  const pct = p && p.total > 0 ? Math.round((p.received / p.total) * 100) : null;
  return h('div', { class: 'item', 'data-id': f.id },
    h('span', { html: ICON.files, style: 'width:16px;margin-top:2px;color:var(--muted)' }),
    h('div', { class: 'grow' },
      h('div', { class: 'title ellipsis' }, f.file_name),
      h('div', { class: 'meta' },
        h('span', { class: `pill ${f.status}` }, f.status.replace('_', ' ')),
        f.folder ? h('span', {}, `📁 ${f.folder}`) : null,
        f.size_bytes ? h('span', {}, fmtBytes(f.size_bytes)) : null,
        h('span', {}, fmtWhen(f.created_at)),
        f.source_page_title ? h('a', { href: f.source_page_url, target: '_blank', class: 'ellipsis' }, f.source_page_title) : null,
      ),
      f.error ? h('div', { class: 'small', style: 'color:var(--err)' }, f.error) : null,
      f.notes ? h('div', { class: 'small', style: 'color:var(--text-2)' }, f.notes) : null,
      pct !== null && p.state === 'in_progress' ? h('div', { class: 'progress' }, h('i', { style: `width:${pct}%` })) : null,
    ),
    h('div', { class: 'actions' },
      f.local_download_id && f.status === 'complete' ? iconBtn('folder', 'Show in folder', () => chrome.downloads.show(f.local_download_id)) : null,
      ['saved', 'failed', 'interrupted'].includes(f.status) ? iconBtn('refresh', 'Download', () => guard(() => redownload(f))) : null,
      iconBtn('edit', 'Edit notes', () => {
        const notes = prompt('Notes for this file', f.notes || '');
        if (notes !== null) guard(async () => { await api(`/api/files/${f.id}`, { method: 'PATCH', body: { notes } }); await refresh(); }, 'Notes saved');
      }),
      iconBtn('trash', 'Forget file', () => confirm(`Forget “${f.file_name}”? (The downloaded file stays on disk.)`) && guard(async () => { await api(`/api/files/${f.id}`, { method: 'DELETE' }); await refresh(); }, 'File forgotten')),
    ),
  );
}

async function redownload(f) {
  const res = await chrome.runtime.sendMessage({ type: 'nova:download', file: f });
  if (!res?.ok) throw new Error(res?.error || 'Download failed');
  toast('Download started');
  await refresh();
}

const card = (title, action, ...body) =>
  h('section', {}, h('div', { class: 'section-title' }, h('h2', {}, title), action), h('div', { class: 'card flush' }, h('div', { class: 'list' }, ...body)));
const empty = (text) => h('div', { class: 'empty-state' }, text);

// ---------------------------------------------------------------- views
function overview() {
  const upcoming = S.reminders.filter((r) => ['pending', 'snoozed'].includes(r.status)).sort((a, b) => new Date(a.next_fire_at) - new Date(b.next_fire_at));
  const endToday = new Date();
  endToday.setHours(23, 59, 59, 999);
  const todayTasks = S.tasks.filter((t) => isOpen(t) && t.due_at && new Date(t.due_at) <= endToday);
  const name = S.user?.name?.split(' ')[0];
  const hour = new Date().getHours();
  const greet = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
  return h('div', {},
    h('div', { class: 'view-head' }, h('div', { class: 'grow' }, h('h1', {}, `${greet}${name ? `, ${name}` : ''}`), h('div', { class: 'muted' }, new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' }))), iconBtn('refresh', 'Refresh', refresh)),
    notConnectedBanner(),
    h('div', { class: 'stats' },
      h('button', { class: 'stat', onclick: () => navigate('tasks') }, h('b', {}, todayTasks.length), h('span', {}, 'Tasks due today')),
      h('button', { class: 'stat', onclick: () => navigate('reminders') }, h('b', {}, upcoming.length), h('span', {}, 'Upcoming reminders')),
      h('button', { class: 'stat', onclick: () => navigate('memory') }, h('b', {}, S.websites.length), h('span', {}, 'Saved websites')),
      h('button', { class: 'stat', onclick: () => navigate('files') }, h('b', {}, S.files.length), h('span', {}, 'Saved files')),
    ),
    card("Today's tasks", h('button', { class: 'btn ghost sm', onclick: () => navigate('tasks') }, 'All tasks →'), ...(todayTasks.length ? todayTasks.slice(0, 6).map(taskRow) : [empty('Nothing due today. 🎉')])),
    card('Upcoming reminders', h('button', { class: 'btn ghost sm', onclick: () => navigate('reminders') }, 'All →'), ...(upcoming.length ? upcoming.slice(0, 5).map(reminderRow) : [empty('No upcoming reminders. Try “Remind me to … tomorrow at 9 AM”.')])),
    card('Recently saved websites', h('button', { class: 'btn ghost sm', onclick: () => navigate('memory') }, 'Memory →'), ...(S.websites.length ? S.websites.slice(0, 4).map(siteRow) : [empty('Say “Remember this website” on any page.')])),
    card('Recent files', h('button', { class: 'btn ghost sm', onclick: () => navigate('files') }, 'Files →'), ...(S.files.length ? S.files.slice(0, 3).map((f) => fileRow(f)) : [empty('Say “Save this PDF” on a page with a document.')])),
  );
}

// ---- assistant
const voice = { active: false, live: null, speaking: false, listenAfter: false };
const GEMINI_VOICES = ['Kore', 'Puck', 'Charon', 'Aoede', 'Leda', 'Orus', 'Zephyr', 'Fenrir', 'Callirrhoe', 'Autonoe', 'Enceladus', 'Iapetus', 'Umbriel', 'Algieba', 'Despina', 'Erinome', 'Algenib', 'Rasalgethi', 'Laomedeia', 'Achernar', 'Alnilam', 'Schedar', 'Gacrux', 'Pulcherrima', 'Achird', 'Zubenelgenubi', 'Vindemiatrix', 'Sadachbia', 'Sadaltager', 'Sulafat'];
let chatBusy = false;

function chatBubble(m) {
  const b = h('div', { class: `bubble ${m.role === 'user' ? 'user' : 'bot'}${m.error ? ' error' : ''}` }, m.content);
  if (m.viaVoice) {
    // Show what NOVA understood, and let the user fix it.
    b.append(h('span', { class: 'heard' }, '🎙 heard'), h('button', { class: 'edit-heard', type: 'button', title: 'Fix what NOVA heard and send again', onclick: () => { const i = $('#chat-input'); if (i) { i.value = m.content; i.focus(); i.select(); } } }, 'Edit & resend'));
  }
  if (!m.trace?.length) return b;
  const tools = m.trace.filter((t) => t.node.startsWith('tool:') || t.node.endsWith('_agent') || t.node === 'conversational_agent');
  return [b, h('div', { class: 'trace' }, ...tools.map((t) => h('span', { class: `pill ${t.ok ? '' : 'failed'}`, title: t.detail }, `${t.ok ? '✓' : '✕'} ${t.node.replace('tool:', '').replace(/_/g, ' ')}`)))];
}

async function loadChatHistory() {
  if (S.chatLoaded || !S.token) return;
  S.chatLoaded = true;
  try {
    const { sessionId } = await chrome.storage.local.get('sessionId');
    if (!sessionId) return;
    const msgs = await api(`/api/conversations/${sessionId}/messages`);
    if (S.chat.length) return; // user already started chatting
    S.chat = msgs.map((m) => ({ role: m.role, content: m.content }));
    if (S.view === 'assistant') render(true);
  } catch {
    /* offline */
  }
}

function assistant() {
  loadChatHistory();
  const log = h('div', { class: 'chat-log', id: 'chat-log', role: 'log', 'aria-live': 'polite' },
    ...(S.chat.length ? S.chat.flatMap((m) => [].concat(chatBubble(m))) : [h('div', { class: 'empty-state' }, h('div', { class: 'orb-mini', style: 'width:44px;height:44px;margin:10px auto 12px' }), h('b', {}, 'How can I help?'), h('div', { class: 'small', style: 'margin-top:6px' }, 'I act on the page you have open. Try a suggestion below.'))]));
  const ctxBar = h('div', { class: 'context-bar', id: 'ctx-bar' }, h('span', { class: 'spinner' }), 'Reading current tab…');
  bg({ type: 'nova:context' }).then(({ page }) => {
    ctxBar.replaceChildren(page?.url ? h('span', { class: 'ellipsis grow', title: page.url }, page.restricted ? '🔒 Browser page — limited access' : `📄 ${page.title || page.url}`) : h('span', {}, 'No web page open'));
  }).catch(() => ctxBar.replaceChildren(h('span', {}, 'No web page open')));
  const status = h('div', { class: 'status-line', id: 'chat-status' });
  const input = h('input', { id: 'chat-input', placeholder: 'Ask NOVA or type a command…', maxlength: 2000, autocomplete: 'off', 'aria-label': 'Message NOVA' });
  const mic = h('button', { class: `mic${voice.active ? ' on' : ''}`, type: 'button', title: 'Speak', 'aria-label': 'Speak to NOVA', html: ICON.mic, onclick: () => (voice.active ? bg({ type: 'nova:dictateStop' }).catch(() => {}) : startVoice()) });
  const form = h('form', { class: 'composer', onsubmit: (e) => { e.preventDefault(); const v = input.value.trim(); if (v) { input.value = ''; sendChat(v); } } }, mic, input, h('button', { class: 'send', type: 'submit', 'aria-label': 'Send', html: ICON.send }));
  const chips = h('div', { class: 'chip-row', id: 'chat-chips' },
    ...['Remember this website', 'Summarize this page', 'Show all my pending tasks', 'What reminders do I have today?', 'Remind me about this after two hours'].map((s) => h('button', { class: 'chip', onclick: () => sendChat(s) }, s)));
  const view = h('div', { class: 'chat' }, h('div', { class: 'view-head' }, h('h1', {}, 'Assistant'), h('button', { class: 'btn ghost sm', onclick: async () => { await newSession(); S.chat = []; render(true); } }, 'New chat')), ctxBar, log, status, chips, form);
  setTimeout(() => (log.scrollTop = log.scrollHeight), 0);
  return view;
}

function setChatStatus(text, spinning = false) {
  const el = $('#chat-status');
  if (el) el.replaceChildren(...(text ? [spinning ? h('span', { class: 'spinner' }) : null, text].filter(Boolean) : []));
}

function pushChat(m) {
  S.chat.push(m);
  const log = $('#chat-log');
  if (!log) return;
  log.querySelector('.empty-state')?.remove();
  log.append(...[].concat(chatBubble(m)));
  log.scrollTop = log.scrollHeight;
}

function setChips(list) {
  const box = $('#chat-chips');
  if (box) box.replaceChildren(...list.map((c) => h('button', { class: 'chip', onclick: c.run }, c.label)));
}

async function sendChat(text, { withText = false, viaVoice = false, quiet = false } = {}) {
  if (chatBusy) return;
  chatBusy = true;
  if (!withText && !quiet) pushChat({ role: 'user', content: text, viaVoice });
  setChips([]);
  setChatStatus(withText ? 'Reading the page…' : 'Thinking…', true);
  try {
    const { page } = await bg({ type: 'nova:context', withText });
    if (withText && page?.text_unavailable) {
      pushChat({ role: 'assistant', content: "I can't read text from this page (Chrome's built-in PDF viewer and some pages block extensions). I can still save or download it.", error: true });
      return;
    }
    setChatStatus('Working…', true);
    const r = await bg({ type: 'nova:ask', message: text, page, pageTextAllowed: withText, channel: viaVoice ? 'voice' : 'text' });
    pushChat({ role: 'assistant', content: r.reply, trace: r.trace, error: r.status === 'error' });
    for (const a of r.actionResults || []) {
      if (!a.ok && !a.deferred) pushChat({ role: 'assistant', content: `Couldn't ${a.type.replace('_', ' ')}: ${a.error}`, error: true });
    }
    const chips = (r.suggestions || []).map((s) => ({ label: s, run: () => sendChat(s, { viaVoice }) }));
    const view = r.client_actions?.find((a) => a.type === 'show_view');
    if (view) chips.push({ label: `Open ${view.payload.view}`, run: () => navigate(view.payload.view === 'memory' ? 'memory' : view.payload.view) });
    if (r.client_actions?.some((a) => a.type === 'request_page_content')) pagePermission(text, viaVoice, page, chips);
    for (const a of r.client_actions || []) {
      if (a.type === 'show_link' && a.payload?.url) chips.unshift({ label: `🔗 ${a.payload.label || 'Open link'}`, run: () => bg({ type: 'nova:openUrl', url: a.payload.url }) });
      if (a.type === 'connect_calendar' && a.payload?.auto) connectCalendar({ viaVoice }); // the user said yes
      else if (a.type === 'connect_calendar') {
        chips.unshift({ label: '📅 Connect Google Calendar', run: () => connectCalendar({ viaVoice }) });
        chips.push({ label: 'Not now', run: () => sendChat('No, don’t connect my calendar', { viaVoice, quiet: true }) });
      }
    }
    setChips(chips);
    // A spoken question gets a spoken answer: listen again as soon as NOVA finishes asking.
    if (viaVoice && S.settings.voice.tts) speak(r.reply, { listen: expectsAnswer(r) });
    loadAll();
  } catch (e) {
    const msg = e.offline ? 'I can’t reach the NOVA server. Check your internet connection and that the server is running.' : e.message;
    pushChat({ role: 'assistant', content: msg, error: true });
    const retry = { label: 'Try again', run: () => sendChat(text, { withText, viaVoice, quiet: true }) };
    setChips(e.status === 401 ? [{ label: 'Open settings', run: () => navigate('settings') }] : [retry]);
    if (viaVoice) speak(msg);
  } finally {
    chatBusy = false;
    setChatStatus('');
  }
}

function pagePermission(text, viaVoice, page, chips) {
  const host = page?.url ? new URL(page.url).hostname : '';
  if (S.settings.pageAccess === 'never') return pushChat({ role: 'assistant', content: 'Page reading is turned off in Settings → Privacy.' });
  if (S.settings.pageAccess === 'always' || S.settings.pageAccessSites.includes(host)) return setTimeout(() => sendChat(text, { withText: true, viaVoice }), 0);
  chips.push({ label: '✓ Allow once', run: () => sendChat(text, { withText: true, viaVoice }) });
  chips.push({ label: `Always on ${host}`, run: async () => { S.settings = await updateSettings({ pageAccessSites: [...new Set([...S.settings.pageAccessSites, host])] }); sendChat(text, { withText: true, viaVoice }); } });
  chips.push({ label: 'Not now', run: () => { setChips([]); pushChat({ role: 'assistant', content: "Okay — I won't read this page." }); } });
}

// ---- voice: recording, recognition (Gemini) and speech all run in the background voice engine.
async function startVoice() {
  if (!S.settings.voice.enabled) return toast('Voice input is disabled in Settings.', true);
  stopSpeaking();
  voice.active = true;
  $('.mic')?.classList.add('on');
  showLive('Listening… speak naturally, I’ll wait until you finish.');
  try {
    const r = await bg({ type: 'nova:dictate' });
    if (!r.ok) throw Object.assign(new Error(r.message || 'Couldn’t start voice input.'), { code: r.error });
  } catch (e) {
    endVoiceUi();
    if (e.code === 'mic_denied') {
      pushChat({ role: 'assistant', content: 'NOVA needs one-time microphone permission. You can keep typing meanwhile.', error: true });
      setChips([{ label: '🎙 Grant microphone access', run: openMicGrant }]);
    } else toast(e.message || 'Voice input failed — type instead.', true);
  }
}

function showLive(text) {
  if (!voice.live) {
    voice.live = h('div', { class: 'bubble user interim live' }, h('span', { class: 'bars', 'aria-hidden': 'true', html: '<i></i><i></i><i></i><i></i><i></i>' }), h('span', { class: 'live-text' }));
    $('#chat-log')?.append(voice.live);
  }
  voice.live.querySelector('.live-text').textContent = text;
  const log = $('#chat-log');
  if (log) log.scrollTop = log.scrollHeight;
}

function endVoiceUi() {
  voice.active = false;
  $('.mic')?.classList.remove('on');
  voice.live?.remove();
  voice.live = null;
  document.documentElement.style.setProperty('--lvl', '0');
  setChatStatus('');
}

function onDictation(m) {
  if (m.phase === 'level') return void document.documentElement.style.setProperty('--lvl', Math.max(0, Math.min(1, Number(m.level) || 0)).toFixed(3));
  if (m.phase === 'partial') return showLive(m.text);
  if (m.phase === 'transcribing') return setChatStatus('Understanding…', true);
  endVoiceUi();
  if (m.phase === 'final') sendChat(m.text, { viaVoice: true });
  else if (m.phase === 'nospeech') toast('I didn’t hear anything — tap the mic and try again.');
  else if (m.phase === 'error') pushChat({ role: 'assistant', content: m.message || 'Voice input failed. You can type instead.', error: true });
}

function speak(text, { listen = false } = {}) {
  if (!S.settings.voice.tts || !text) return;
  voice.speaking = true;
  voice.listenAfter = listen;
  bg({ type: 'nova:speak', text }).catch(() => {});
}

function onSpeech(m) {
  const status = $('#chat-status');
  if (m.state === 'start') {
    voice.speaking = true;
    status?.replaceChildren(h('span', { class: 'bars', 'aria-hidden': 'true', html: '<i></i><i></i><i></i><i></i><i></i>' }), ' Speaking… ', h('button', { class: 'btn ghost sm', onclick: () => { stopSpeaking(); setChatStatus(''); } }, '■ Stop'));
  } else if (m.state === 'level') document.documentElement.style.setProperty('--lvl', Math.max(0, Math.min(1, Number(m.level) || 0)).toFixed(3));
  else if (m.state === 'end') {
    voice.speaking = false;
    document.documentElement.style.setProperty('--lvl', '0');
    setChatStatus('');
    const listen = voice.listenAfter && m.outcome !== 'interrupted' && !voice.active && S.view === 'assistant';
    voice.listenAfter = false;
    if (listen) startVoice();
  }
}

function stopSpeaking() {
  voice.listenAfter = false; // the user cut NOVA off: don't open the mic on them
  if (voice.speaking) bg({ type: 'nova:ttsStop' }).catch(() => {});
  voice.speaking = false;
}
addEventListener('keydown', (e) => e.key === 'Escape' && voice.speaking && (stopSpeaking(), setChatStatus('')));

chrome.runtime.onMessage.addListener((m) => {
  if (m?.type === 'nova:dictation') onDictation(m);
  else if (m?.type === 'nova:speech') onSpeech(m);
  return false;
});

// ---- Google Calendar (OAuth runs between Google and the NOVA server; we only open the consent page)
async function connectCalendar({ viaVoice = false, resume = true } = {}) {
  let attempt;
  try {
    attempt = (await bg({ type: 'nova:calendarConnect' })).attempt_id;
  } catch (e) {
    return pushChat({ role: 'assistant', content: e.status === 503 ? 'Google Calendar isn’t set up on your NOVA server yet (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET). See the README.' : e.message, error: true });
  }
  toast('Approve access in the Google tab — NOVA will continue here.');
  const started = Date.now();
  const poll = async () => {
    let st;
    try {
      st = await bg({ type: 'nova:calendarStatus', attempt });
    } catch {
      return setTimeout(poll, 3000);
    }
    const outcome = st.attempt?.outcome;
    if (outcome === 'connected') {
      toast(`Google Calendar connected${st.account ? ` (${st.account})` : ''}`);
      if (S.view === 'settings') render();
      if (resume) {
        if (S.view !== 'assistant') navigate('assistant');
        sendChat('I’ve connected my Google Calendar.', { viaVoice });
      }
      return;
    }
    if (outcome === 'denied') return pushChat({ role: 'assistant', content: st.attempt.detail === 'scope_not_granted' ? 'Calendar access wasn’t ticked on Google’s page, so nothing was connected.' : 'You didn’t allow access, so Google Calendar isn’t connected. Nothing was changed.' });
    if (outcome === 'error' || outcome === 'expired' || Date.now() - started > 10 * 60000) return toast('Connecting Google Calendar didn’t finish. Try again.', true);
    setTimeout(poll, 2000);
  };
  setTimeout(poll, 2000);
}

function calendarCard() {
  const body = h('div', { class: 'setting', id: 'calendar-status' }, h('div', { class: 'grow small muted' }, S.token ? 'Checking…' : 'Connect your NOVA account first.'));
  if (S.token) {
    bg({ type: 'nova:calendarStatus' }).then((st) => {
      if (!st.configured) return body.replaceChildren(h('div', { class: 'grow small muted' }, 'Not set up on your NOVA server yet. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to backend/.env (see README → Google Calendar).'));
      if (st.connected) {
        body.replaceChildren(
          h('div', { class: 'grow' }, h('div', {}, h('span', { class: 'pill ok' }, 'Connected'), st.account ? ` ${st.account}` : ''), h('div', { class: 'muted small' }, 'NOVA can add events to your primary calendar and check for clashes. It always confirms before creating anything.')),
          h('button', { class: 'btn sm', onclick: () => guard(async () => { await bg({ type: 'nova:calendarDisconnect' }); render(); }, 'Google Calendar disconnected') }, 'Disconnect'),
        );
      } else {
        body.replaceChildren(
          h('div', { class: 'grow' }, h('div', {}, 'Not connected'), h('div', { class: 'muted small' }, 'Lets you say “schedule a meeting tomorrow at 11”. NOVA asks Google only for permission to manage calendar events.')),
          h('button', { class: 'btn sm primary', id: 'calendar-connect', onclick: () => connectCalendar({ resume: false }) }, 'Connect Google Calendar'),
        );
      }
    }).catch(() => body.replaceChildren(h('div', { class: 'grow small muted' }, 'Status unavailable (server offline?)')));
  }
  return card('Google Calendar', null, body);
}

// ---- reminders
function reminders() {
  const tab = S.filters.reminders;
  const groups = {
    upcoming: S.reminders.filter((r) => r.status === 'pending'),
    snoozed: S.reminders.filter((r) => r.status === 'snoozed'),
    completed: S.reminders.filter((r) => ['completed', 'fired'].includes(r.status)),
    cancelled: S.reminders.filter((r) => r.status === 'cancelled'),
  };
  const list = groups[tab].sort((a, b) => (tab === 'upcoming' || tab === 'snoozed' ? 1 : -1) * (new Date(a.next_fire_at) - new Date(b.next_fire_at)));
  const form = h('form', { class: 'card inline', id: 'new-reminder' },
    h('h3', {}, 'New reminder'),
    h('input', { class: 'input', name: 'title', placeholder: 'What should I remind you about?', required: true, maxlength: 300 }),
    h('div', { class: 'form-grid' },
      h('input', { class: 'input', type: 'datetime-local', name: 'when', required: true, value: toLocalInput(new Date(Date.now() + 3600000).toISOString()) }),
      h('select', { class: 'input', name: 'kind' }, h('option', { value: 'reminder' }, 'Reminder'), h('option', { value: 'alarm' }, 'Alarm'), h('option', { value: 'meeting' }, 'Meeting (10 min heads-up)')),
      recurrenceSelect(''),
      h('button', { class: 'btn primary', type: 'submit' }, 'Add reminder'),
    ),
  );
  form.onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(form);
    const kind = f.get('kind');
    guard(async () => {
      await api('/api/reminders', { method: 'POST', body: { title: f.get('title'), scheduled_at: fromLocalInput(f.get('when')), kind, recurrence: f.get('recurrence') || null, notify_offsets: kind === 'meeting' ? [10, 0] : [0], timezone: Intl.DateTimeFormat().resolvedOptions().timeZone } });
      await afterReminderChange();
    }, 'Reminder scheduled');
  };
  return h('div', {},
    h('div', { class: 'view-head' }, h('h1', {}, 'Reminders'), iconBtn('refresh', 'Sync now', () => guard(afterReminderChange, 'Synced'))),
    notConnectedBanner(),
    h('div', { class: 'tabs', role: 'tablist' }, ...Object.keys(groups).map((k) => h('button', { class: `tab${tab === k ? ' active' : ''}`, role: 'tab', 'aria-selected': String(tab === k), onclick: () => { S.filters.reminders = k; render(); } }, `${k[0].toUpperCase()}${k.slice(1)} ${groups[k].length ? `(${groups[k].length})` : ''}`))),
    h('div', { class: 'card flush' }, h('div', { class: 'list', id: 'reminder-list' }, ...(list.length ? list.map(reminderRow) : [empty(`No ${tab} reminders.`)]))),
    form,
    h('div', { class: 'banner info small' }, h('div', {}, h('b', {}, 'How alarms work: '), 'NOVA uses Chrome alarms and notifications. They ring while Chrome is running (even in the background). If Chrome is closed or your computer is asleep, NOVA shows a “missed” notification the next time Chrome starts — it cannot wake your computer.')),
  );
}

// ---- tasks
function tasks() {
  const f = S.filters.tasks;
  const endToday = new Date();
  endToday.setHours(23, 59, 59, 999);
  let list = S.tasks.filter((t) => (f.status === 'open' ? isOpen(t) : f.status === 'completed' ? t.status === 'completed' : true));
  if (f.priority) list = list.filter((t) => t.priority === f.priority);
  if (f.due === 'today') list = list.filter((t) => t.due_at && new Date(t.due_at) <= endToday);
  if (f.due === 'upcoming') list = list.filter((t) => t.due_at && new Date(t.due_at) > new Date());
  if (f.due === 'overdue') list = list.filter(isOverdue);
  if (f.due === 'none') list = list.filter((t) => !t.due_at);
  const sel = (key, opts) => h('select', { class: 'input', 'aria-label': key, onchange: (e) => { S.filters.tasks[key] = e.target.value; render(); } }, ...opts.map(([v, l]) => h('option', { value: v, selected: f[key] === v }, l)));
  const form = h('form', { class: 'card inline', id: 'new-task' },
    h('input', { class: 'input', name: 'title', placeholder: 'Add a task…', required: true, maxlength: 300 }),
    h('div', { class: 'form-grid' }, h('input', { class: 'input', type: 'datetime-local', name: 'due', 'aria-label': 'Due' }), prioritySelect(), h('button', { class: 'btn primary', type: 'submit' }, 'Add task')),
  );
  form.onsubmit = (e) => {
    e.preventDefault();
    const d = new FormData(form);
    guard(async () => { await api('/api/tasks', { method: 'POST', body: { title: d.get('title'), priority: d.get('priority'), due_at: fromLocalInput(d.get('due')), due_has_time: !!d.get('due') } }); await refresh(); }, 'Task added');
  };
  return h('div', {},
    h('div', { class: 'view-head' }, h('h1', {}, 'Tasks'), iconBtn('refresh', 'Refresh', refresh)),
    notConnectedBanner(),
    form,
    h('div', { class: 'toolbar' },
      sel('status', [['open', 'Open'], ['completed', 'Completed'], ['all', 'All']]),
      sel('priority', [['', 'Any priority'], ['urgent', 'Urgent'], ['high', 'High'], ['medium', 'Medium'], ['low', 'Low']]),
      sel('due', [['', 'Any date'], ['today', 'Due today'], ['upcoming', 'Upcoming'], ['overdue', 'Overdue'], ['none', 'No due date']]),
    ),
    h('div', { class: 'card flush' }, h('div', { class: 'list', id: 'task-list' }, ...(list.length ? list.map(taskRow) : [empty('No tasks match these filters.')]))),
  );
}

// ---- memory
let memoryTimer = null;
function memory() {
  const f = S.filters.memory;
  const categories = [...new Set(S.websites.map((s) => s.category).filter(Boolean))].sort();
  const listBox = h('div', { class: 'list', id: 'site-list' });
  const draw = (sites) => listBox.replaceChildren(...(sites.length ? sites.map(siteRow) : [empty(f.q || f.period ? 'No saved websites match.' : 'Nothing saved yet. Say “Remember this website” on any page.')]));
  const search = async () => {
    if (!f.q && !f.period) return draw(S.websites.filter((s) => !f.category || s.category === f.category));
    const sites = await guard(() => api(`/api/websites${qs({ q: f.q, period: f.period, limit: 100 })}`));
    if (sites) draw(sites.filter((s) => !f.category || s.category === f.category));
  };
  const input = h('input', { class: 'input', type: 'search', placeholder: 'Search saved websites (e.g. LangGraph)…', value: f.q, 'aria-label': 'Search websites', oninput: (e) => { f.q = e.target.value; clearTimeout(memoryTimer); memoryTimer = setTimeout(search, 250); } });
  const period = h('select', { class: 'input', 'aria-label': 'Period', onchange: (e) => { f.period = e.target.value; search(); } }, ...[['', 'Any time'], ['today', 'Today'], ['yesterday', 'Yesterday'], ['this week', 'This week'], ['last week', 'Last week'], ['last month', 'Last month']].map(([v, l]) => h('option', { value: v, selected: f.period === v }, l)));
  search();
  return h('div', {},
    h('div', { class: 'view-head' }, h('h1', {}, 'Website Memory'), h('button', { class: 'btn sm', onclick: saveCurrentPage }, '+ Save current page')),
    notConnectedBanner(),
    h('div', { class: 'toolbar' }, input, period),
    categories.length ? h('div', { class: 'chip-row' }, h('button', { class: `chip${!f.category ? ' active' : ''}`, onclick: () => { f.category = ''; render(); } }, 'All'), ...categories.map((c) => h('button', { class: `chip${f.category === c ? ' active' : ''}`, onclick: () => { f.category = c; render(); } }, c))) : null,
    h('div', { class: 'card flush' }, listBox),
    h('div', { class: 'muted small' }, 'NOVA only remembers pages you explicitly save — it never records your general browsing history.'),
  );
}

async function saveCurrentPage() {
  await guard(async () => {
    const { page } = await bg({ type: 'nova:context' });
    if (!page?.url || page.restricted) throw new Error('Open a regular web page first.');
    await api('/api/websites', { method: 'POST', body: { url: page.url, title: page.title || '' } });
    await refresh();
  }, 'Page saved to memory');
}

// ---- files
let progressTimer = null;
function files() {
  const f = S.filters.files;
  const listBox = h('div', { class: 'list', id: 'file-list' });
  const draw = (items, progress) => listBox.replaceChildren(...(items.length ? items.map((x) => fileRow(x, progress)) : [empty(f.q ? 'No files match.' : 'No files yet. Say “Save this PDF” on a page with a document.')]));
  const matches = () => S.files.filter((x) => !f.q || `${x.file_name} ${x.notes || ''} ${x.source_page_title || ''} ${x.folder || ''}`.toLowerCase().includes(f.q.toLowerCase()));
  draw(matches(), []);
  clearInterval(progressTimer);
  const poll = async () => {
    if (S.view !== 'files') return clearInterval(progressTimer);
    const progress = await bg({ type: 'nova:downloads' }).catch(() => []);
    const active = progress.some((p) => p.state === 'in_progress');
    if (active || S.files.some((x) => ['requested', 'in_progress'].includes(x.status))) {
      if (!active) await loadAll();
      draw(matches(), progress);
    }
  };
  progressTimer = setInterval(poll, 1000);
  return h('div', {},
    h('div', { class: 'view-head' }, h('h1', {}, 'Saved Files'), iconBtn('refresh', 'Refresh', refresh)),
    notConnectedBanner(),
    h('div', { class: 'toolbar' }, h('input', { class: 'input', type: 'search', placeholder: 'Search files and notes…', value: f.q, 'aria-label': 'Search files', oninput: (e) => { f.q = e.target.value; draw(matches(), []); } })),
    h('div', { class: 'card flush' }, listBox),
    h('div', { class: 'muted small' }, 'Downloads go to your browser’s Downloads folder (under “NOVA/”). NOVA can’t read or move other files on your computer.'),
  );
}

// ---- settings
function toggle(label, desc, checked, onchange, id) {
  return h('div', { class: 'setting' },
    h('div', { class: 'grow' }, h('div', {}, label), desc ? h('div', { class: 'muted small' }, desc) : null),
    h('label', { class: 'toggle' }, h('input', { type: 'checkbox', checked, id, 'aria-label': label, onchange: (e) => onchange(e.target.checked) }), h('span', {})),
  );
}
const setting = (label, desc, control) => h('div', { class: 'setting' }, h('div', { class: 'grow' }, h('div', {}, label), desc ? h('div', { class: 'muted small' }, desc) : null), control);
const save = async (patch, msg = 'Saved') => { S.settings = await updateSettings(patch); applyTheme(); toast(msg); };

function accountSection() {
  if (S.token && S.user) {
    return card('Account', null,
      setting(S.user.name, `${S.user.email} · ${S.user.timezone}`, h('button', { class: 'btn sm', onclick: async () => { if (confirm('Sign out NOVA on this browser? Your data stays on the server.')) { await chrome.storage.local.remove(['token', 'user', 'nova.reminders']); await bg({ type: 'nova:rebuild' }).catch(() => {}); await boot(); } } }, 'Sign out')),
      setting('Server', S.backendUrl, h('div', { class: 'row' },
        h('span', { class: `pill ${S.online ? 'ok' : 'failed'}` }, S.online ? 'connected' : 'offline'),
        h('button', { class: 'btn sm', onclick: changeServer }, 'Change'))),
    );
  }
  const urlInput = h('input', { class: 'input', name: 'backend', value: S.backendUrl, required: true, 'aria-label': 'Server URL' });
  const reg = h('form', { class: 'stack', id: 'register-form' },
    h('label', { class: 'field' }, 'NOVA server URL', urlInput),
    h('div', { class: 'form-grid' }, h('label', { class: 'field' }, 'Your name', h('input', { class: 'input', name: 'name', required: true, maxlength: 120 })), h('label', { class: 'field' }, 'Email', h('input', { class: 'input', name: 'email', type: 'email', required: true }))),
    h('label', { class: 'field' }, 'Registration secret (only if your server requires one)', h('input', { class: 'input', name: 'secret', type: 'password' })),
    h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'submit' }, 'Create account & connect')),
  );
  reg.onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(reg);
    guard(async () => {
      const backendUrl = await useBackend(f.get('backend'));
      const out = await api('/api/auth/register', { method: 'POST', body: { name: f.get('name'), email: f.get('email'), timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, device_name: 'chrome-extension', registration_secret: f.get('secret') || null }, state: { backendUrl } });
      await chrome.storage.local.set({ token: out.token, user: out.user });
      await boot();
    }, 'Connected! NOVA is ready.');
  };
  const tok = h('form', { class: 'row', id: 'token-form' }, h('input', { class: 'input', name: 'token', placeholder: 'Paste an existing token (nova_…)', required: true }), h('button', { class: 'btn', type: 'submit' }, 'Use token'));
  tok.onsubmit = (e) => {
    e.preventDefault();
    const token = new FormData(tok).get('token').trim();
    guard(async () => {
      const backendUrl = await useBackend(urlInput.value);
      const user = await api('/api/auth/me', { state: { backendUrl, token } });
      await chrome.storage.local.set({ token, user });
      await boot();
    }, 'Connected with existing token');
  };
  return h('section', { class: 'stack' }, h('div', { class: 'section-title' }, h('h2', {}, 'Connect to your NOVA server')), h('div', { class: 'card stack' }, reg, h('div', { class: 'muted small' }, 'Already registered on another browser?'), tok));
}

// Move to another server (e.g. localhost → a deployed one) without signing out. The account
// carries over when both servers share a database; otherwise the user is asked to sign in again.
async function changeServer() {
  const raw = prompt('New NOVA server URL (for example https://nova-api.onrender.com):', S.backendUrl);
  if (!raw || raw.trim().replace(/\/+$/, '') === S.backendUrl) return;
  const previous = S.backendUrl;
  guard(async () => {
    try {
      await useBackend(raw);
    } catch (e) {
      await chrome.storage.local.set({ backendUrl: previous });
      S.backendUrl = previous;
      throw e.offline ? new Error(`Couldn't reach ${raw.trim()}. If it's on a free plan it may be waking up, so try again in a minute.`) : e;
    }
    try {
      const user = await api('/api/auth/me');
      await chrome.storage.local.set({ user });
    } catch (e) {
      if (e.status !== 401) throw e;
      // This server doesn't know the account: sign out locally so the connect form appears.
      await chrome.storage.local.remove(['token', 'user', 'nova.reminders']);
      toast('That server doesn’t have your account. Create one (or paste a token) below.', true);
    }
    await bg({ type: 'nova:rebuild' }).catch(() => {});
    await boot();
  }, 'Server changed');
}

async function useBackend(raw) {
  let url;
  try {
    url = new URL(raw.trim());
  } catch {
    throw new Error('Enter a valid server URL, e.g. http://localhost:8000');
  }
  const origin = url.origin;
  const builtIn = ['http://localhost:8000', 'http://127.0.0.1:8000'].includes(origin);
  if (!builtIn) {
    const granted = await chrome.permissions.request({ origins: [`${origin}/*`] });
    if (!granted) throw new Error('NOVA needs permission to contact that server.');
  }
  await chrome.storage.local.set({ backendUrl: origin });
  S.backendUrl = origin;
  await api('/api/health', { state: { backendUrl: origin } });
  return origin;
}

async function micGranted() {
  try {
    return (await navigator.permissions.query({ name: 'microphone' })).state === 'granted';
  } catch {
    return false;
  }
}

const openMicGrant = () => chrome.tabs.create({ url: chrome.runtime.getURL('src/permission/mic.html') });

async function enableWakeWord(on) {
  if (on && !S.settings.voice.enabled) await updateSettings({ voice: { enabled: true } });
  await save({ voice: { wakeWord: on } }, on ? '“Hey NOVA” is on' : '“Hey NOVA” is off');
  if (on && !(await micGranted())) openMicGrant();
  render();
}

// Live status of the background listener (written by the service worker to storage.session).
function wakeStatusLine() {
  const line = h('div', { class: 'setting', id: 'wake-status' });
  const paint = (ws) => {
    if (!S.settings.voice.enabled || !S.settings.voice.wakeWord) return void (line.hidden = true);
    line.hidden = false;
    const st = ws?.state || 'starting';
    const label = { on: ws?.mode === 'wake' ? 'Listening for “Hey NOVA”' : 'Active', loading: 'Loading the wake-word model…', off: 'Starting…', starting: 'Starting…', error: 'Not listening' }[st] || st;
    const pill = h('span', { class: `pill ${st === 'on' ? 'ok' : st === 'error' ? 'failed' : 'snoozed'}` }, label);
    const fix = ws?.error === 'mic_denied' ? h('button', { class: 'btn sm', onclick: openMicGrant }, 'Grant microphone') : null;
    line.replaceChildren(h('div', { class: 'grow small' }, pill, ws?.message ? h('div', { class: 'muted small', style: 'margin-top:4px' }, ws.message) : null), fix || '');
  };
  chrome.storage.session.get('wakeStatus').then(({ wakeStatus }) => paint(wakeStatus));
  const onChange = (changes, area) => {
    if (!line.isConnected) return chrome.storage.onChanged.removeListener(onChange);
    if (area === 'session' && changes.wakeStatus) paint(changes.wakeStatus.newValue);
  };
  chrome.storage.onChanged.addListener(onChange);
  return line;
}

function settings() {
  const st = S.settings;
  const voices = globalThis.speechSynthesis?.getVoices() || [];
  const providerSel = h('select', { class: 'input', style: 'width:auto', 'aria-label': 'Preferred AI provider', onchange: (e) => guard(async () => { S.user = await api('/api/auth/me', { method: 'PATCH', body: { preferences: { provider: e.target.value } } }); await chrome.storage.local.set({ user: S.user }); }, 'Provider preference saved') },
    ...[['', 'Server default'], ['gemini', 'Gemini first'], ['groq', 'Groq first']].map(([v, l]) => h('option', { value: v, selected: (S.user?.preferences?.provider || '') === v }, l)));
  const health = h('div', { class: 'small muted', id: 'provider-health' }, S.token ? 'Checking providers…' : '');
  if (S.token) api('/api/health/providers').then((p) => health.replaceChildren(...Object.entries(p).map(([n, v]) => h('span', { class: `pill ${v.configured ? (v.cooling_down ? 'snoozed' : 'ok') : 'failed'}`, style: 'margin-right:6px', title: v.last_error || '' }, `${n}: ${v.configured ? (v.cooling_down ? 'rate-limited' : v.model) : 'not configured'}`)))).catch(() => health.replaceChildren('Provider status unavailable'));

  return h('div', {},
    h('div', { class: 'view-head' }, h('h1', {}, 'Settings')),
    accountSection(),
    card('Assistant visibility', null,
      toggle('Show the floating NOVA orb', 'Appears in the bottom-right of web pages. Drag it anywhere.', st.orbEnabled, (v) => save({ orbEnabled: v }), 'set-orb'),
      toggle('Animations', 'Turn off for a calmer, static orb.', st.animations, (v) => save({ animations: v })),
      toggle('Contextual suggestions', 'At most one small hint per site per day (uses only URL and title).', st.suggestions, (v) => save({ suggestions: v })),
      setting('Hidden on these sites', st.disabledSites.length ? st.disabledSites.join(', ') : 'None', h('button', { class: 'btn sm', onclick: async () => { const { page } = await bg({ type: 'nova:context' }).catch(() => ({})); const host = page?.url ? new URL(page.url).hostname : prompt('Hide NOVA on which site? (e.g. docs.google.com)'); if (host) save({ disabledSites: st.disabledSites.includes(host) ? st.disabledSites.filter((x) => x !== host) : [...st.disabledSites, host] }, st.disabledSites.includes(host) ? `NOVA shown on ${host}` : `NOVA hidden on ${host}`).then(render); } }, 'Toggle current site')),
      setting('Keyboard shortcut', h('span', {}, h('span', { class: 'kbd' }, 'Alt+Shift+N'), ' talk · ', h('span', { class: 'kbd' }, 'Alt+Shift+D'), ' dashboard'), h('button', { class: 'btn sm', onclick: () => chrome.tabs.create({ url: 'chrome://extensions/shortcuts' }) }, 'Customize')),
    ),
    card('Voice', null,
      toggle('Voice input', 'Talk to NOVA with the mic button or the shortcut. Speech is transcribed by Gemini on your NOVA server (Groq Whisper as backup), tuned for Indian and US English. Turning this off also turns off “Hey NOVA”.', st.voice.enabled, (v) => save({ voice: { enabled: v } }).then(render)),
      toggle('“Hey NOVA” hands-free', 'NOVA listens on this computer for its name, then takes your command. Wake-word detection runs entirely on your device; audio goes to your NOVA server only after it hears “Hey NOVA”. Say “Hey NOVA” while it’s talking to interrupt.', st.voice.enabled && st.voice.wakeWord, (v) => enableWakeWord(v), 'set-wake'),
      wakeStatusLine(),
      toggle('Conversation mode', 'After answering a hands-free command, keep listening for a follow-up (“make it 6 instead”). Stop talking or say “that’s all” to end.', st.voice.conversation, (v) => save({ voice: { conversation: v } })),
      toggle('Speak replies', 'Read answers aloud after voice commands. Press Esc, the stop button or the orb to stop.', st.voice.tts, (v) => save({ voice: { tts: v } })),
      setting('Reply voice', 'Natural uses Gemini text-to-speech on your server; the browser voice is used automatically if it’s unavailable.',
        h('select', { class: 'input', style: 'width:auto', id: 'set-engine', onchange: (e) => save({ voice: { engine: e.target.value } }).then(render) }, ...[['gemini', 'Natural (Gemini)'], ['browser', 'Browser voice']].map(([v, l]) => h('option', { value: v, selected: (st.voice.engine || 'gemini') === v }, l)))),
      (st.voice.engine || 'gemini') === 'gemini'
        ? setting('Natural voice', null, h('div', { class: 'row', style: 'gap:6px' },
          h('select', { class: 'input', style: 'width:auto', id: 'set-gemini-voice', onchange: (e) => save({ voice: { geminiVoice: e.target.value } }) }, ...GEMINI_VOICES.map((v) => h('option', { value: v, selected: (st.voice.geminiVoice || 'Kore') === v }, v))),
          h('button', { class: 'btn sm', onclick: () => speak('Hi, I’m NOVA. This is how I’ll sound when I answer you.') }, 'Preview')))
        : setting('Browser voice', null, h('select', { class: 'input', style: 'width:auto;max-width:200px', onchange: (e) => save({ voice: { voiceName: e.target.value } }) }, h('option', { value: '' }, 'System default'), ...voices.map((v) => h('option', { value: v.name, selected: st.voice.voiceName === v.name }, `${v.name} (${v.lang})`)))),
      (st.voice.engine || 'gemini') === 'browser' ? setting('Speaking rate', `${st.voice.rate}×`, h('input', { type: 'range', min: 0.6, max: 1.6, step: 0.1, value: st.voice.rate, onchange: (e) => save({ voice: { rate: Number(e.target.value) } }).then(render) })) : null,
      setting('Microphone', 'Grant once — it then works on every website and in this dashboard.', h('button', { class: 'btn sm', onclick: openMicGrant }, 'Grant access')),
    ),
    calendarCard(),
    card('AI provider', null,
      setting('Preferred provider', 'If it fails or is rate-limited, NOVA switches to the other automatically. API keys stay on the server.', providerSel),
      h('div', { class: 'setting' }, health),
    ),
    card('Notifications', null,
      toggle('Keep alarms on screen until dismissed', 'Alarms and meetings stay visible until you act on them.', st.notifications.requireInteraction, (v) => save({ notifications: { requireInteraction: v } })),
      setting('Test notifications', 'Make sure Chrome is allowed to show notifications in your OS settings.', h('button', { class: 'btn sm', onclick: () => guard(() => bg({ type: 'nova:testNotification' }), 'Test notification sent') }, 'Send test')),
    ),
    card('Privacy', null,
      setting('Reading page text', 'Needed only to summarize/explain pages. URL and title are always used for context.',
        h('select', { class: 'input', style: 'width:auto', id: 'set-page-access', onchange: (e) => save({ pageAccess: e.target.value }) }, ...[['ask', 'Ask every time'], ['always', 'Always allow'], ['never', 'Never']].map(([v, l]) => h('option', { value: v, selected: st.pageAccess === v }, l)))),
      setting('Sites allowed to be read', st.pageAccessSites.length ? st.pageAccessSites.join(', ') : 'None', st.pageAccessSites.length ? h('button', { class: 'btn sm', onclick: () => save({ pageAccessSites: [] }, 'Cleared').then(render) }, 'Clear') : h('span')),
      setting('Export my data', 'Download everything NOVA stores about you as JSON.', h('button', { class: 'btn sm', disabled: !S.token, onclick: exportData }, 'Export')),
      setting('Delete all my data', 'Erases reminders, tasks, saved websites, files and conversations from the server.', h('button', { class: 'btn sm danger', disabled: !S.token, onclick: deleteData }, 'Delete…')),
      h('div', { class: 'setting small muted', style: 'display:block' },
        h('b', {}, 'What is stored where. '),
        'On your NOVA server: your account, reminders, tasks, websites you saved, file metadata and conversation messages. In this browser: settings, your device token and a cache of reminders so alarms work offline. Sent to the AI provider (Gemini/Groq) per request: your message plus the page title/domain — and page text only when you allow it. Voice: your recorded command is sent for transcription and replies for speech synthesis, with storage turned off. Google Calendar tokens (if you connect it) are encrypted on your NOVA server and never sent to this browser. Webpage content is treated as untrusted data and can never trigger actions.'),
    ),
    card('Appearance', null,
      setting('Theme', null, h('div', { class: 'tabs' }, ...['auto', 'dark', 'light'].map((t) => h('button', { class: `tab${st.theme === t ? ' active' : ''}`, onclick: () => save({ theme: t }).then(render) }, t[0].toUpperCase() + t.slice(1))))),
    ),
    card('Limitations', null, h('div', { class: 'setting small muted', style: 'display:block' },
      '• Alarms and reminders ring only while Chrome is running; missed ones are shown when Chrome starts again. • Chrome-internal pages (chrome://, Web Store) and the built-in PDF viewer can’t run extensions, so NOVA uses only the tab title/URL there. • Downloads are saved under your Downloads folder; NOVA can’t access other files. • Voice needs your NOVA server: audio is recorded only after you press the mic (or say “Hey NOVA”) and is transcribed by Gemini (or Groq) with storage turned off. • Google Calendar: NOVA can add events to your primary calendar; it doesn’t edit or delete existing events.')),
  );
}

async function exportData() {
  await guard(async () => {
    const data = await api('/api/data/export');
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
    const a = h('a', { href: url, download: `nova-export-${new Date().toISOString().slice(0, 10)}.json` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }, 'Export downloaded');
}

async function deleteData() {
  const answer = prompt('This permanently deletes all your NOVA data on the server. Type DELETE to confirm.');
  if (answer !== 'DELETE') return;
  await guard(async () => {
    await api('/api/data?confirm=DELETE', { method: 'DELETE' });
    await chrome.storage.local.remove(['nova.reminders', 'nova.savedUrls']);
    await bg({ type: 'nova:rebuild' }).catch(() => {});
    S.chat = [];
    await refresh();
  }, 'All your data was deleted');
}

// ---------------------------------------------------------------- boot
async function boot() {
  const st = await getState();
  Object.assign(S, { settings: st.settings, token: st.token, user: st.user, backendUrl: st.backendUrl });
  applyTheme();
  const { dashboardView } = await chrome.storage.session.get('dashboardView').catch(() => ({}));
  if (dashboardView) await chrome.storage.session.remove('dashboardView');
  const hashView = location.hash.slice(1);
  S.view = MODE === 'options' ? 'settings' : dashboardView || (VIEWS.some(([v]) => v === hashView) ? hashView : S.token ? 'overview' : 'settings');
  renderNav();
  render();
  if (S.token) {
    try {
      S.user = await api('/api/auth/me');
      await chrome.storage.local.set({ user: S.user });
      S.online = true;
    } catch (e) {
      S.online = e.status === 401 ? null : false;
    }
    await loadAll();
    render();
  }
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return;
  if (changes.settings) {
    S.settings = { ...S.settings, ...changes.settings.newValue };
    applyTheme();
  }
  if (changes['nova.reminders'] && S.token && ['overview', 'reminders'].includes(S.view)) loadAll().then(() => !document.querySelector('form.inline:focus-within') && render());
});
chrome.storage.session.onChanged?.addListener((changes) => {
  if (changes.dashboardView?.newValue) {
    chrome.storage.session.remove('dashboardView');
    navigate(changes.dashboardView.newValue);
  }
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
globalThis.speechSynthesis?.addEventListener?.('voiceschanged', () => S.view === 'settings' && render());

boot();
globalThis.novaApp = { S, navigate, refresh, sendChat };
