// Pure scheduling logic (no chrome.* calls) so it can be unit-tested in Node.
//
// Reminders are persisted on the backend AND cached in chrome.storage. Alarms are derived
// state: on every service-worker start / browser startup / sync we recompute the full alarm
// plan from the cache, so suspended workers, restarts and extension reloads never lose them.

export const ALARM_PREFIX = 'nova:r:';
export const SYNC_ALARM = 'nova:sync';
export const MISSED_WINDOW_MS = 24 * 60 * 60 * 1000;
const ACTIVE = new Set(['pending', 'snoozed']);

export function alarmName(id, offset, fireAtMs) {
  return `${ALARM_PREFIX}${id}:${offset}:${fireAtMs}`;
}

export function parseAlarmName(name) {
  if (!name || !name.startsWith(ALARM_PREFIX)) return null;
  const [id, offset, fireAt] = name.slice(ALARM_PREFIX.length).split(':').map(Number);
  if (![id, offset, fireAt].every(Number.isFinite)) return null;
  return { id, offset, fireAt };
}

export function fireTime(reminder) {
  return Date.parse(reminder.next_fire_at || reminder.scheduled_at);
}

/**
 * @returns {{alarms: {name:string, when:number, id:number, offset:number}[], missed: {reminder:object, name:string}[]}}
 */
export function planAlarms(reminders, nowMs, delivered = new Set()) {
  const alarms = [];
  const missed = [];
  for (const r of reminders || []) {
    if (!ACTIVE.has(r.status)) continue;
    const fireAt = fireTime(r);
    if (!Number.isFinite(fireAt)) continue;
    const offsets = Array.isArray(r.notify_offsets) && r.notify_offsets.length ? r.notify_offsets : [0];
    for (const offset of offsets) {
      const name = alarmName(r.id, offset, fireAt);
      if (delivered.has(name)) continue;
      const when = fireAt - offset * 60000;
      if (when > nowMs) alarms.push({ name, when, id: r.id, offset });
      else if (offset === 0 && nowMs - when <= MISSED_WINDOW_MS) missed.push({ reminder: r, name });
    }
  }
  alarms.sort((a, b) => a.when - b.when);
  return { alarms, missed };
}

/** Diff the desired plan against existing chrome alarms. */
export function diffAlarms(existing, plan) {
  const want = new Map(plan.map((a) => [a.name, a]));
  const have = new Set(existing.filter((a) => a.name.startsWith(ALARM_PREFIX)).map((a) => a.name));
  return {
    create: plan.filter((a) => !have.has(a.name)),
    clear: [...have].filter((n) => !want.has(n)),
  };
}

export function formatTime(ms, now = Date.now()) {
  const d = new Date(ms);
  const t = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const day = new Date(now);
  const sameDay = d.toDateString() === day.toDateString();
  const tomorrow = new Date(now + 86400000).toDateString() === d.toDateString();
  if (sameDay) return `today at ${t}`;
  if (tomorrow) return `tomorrow at ${t}`;
  return `${d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })} at ${t}`;
}

export function notificationFor(reminder, offset, { missed = false, requireInteraction = true, now = Date.now() } = {}) {
  const kindLabel = { alarm: '⏰ Alarm', meeting: '📅 Meeting', reminder: '🔔 Reminder' }[reminder.kind] || '🔔 Reminder';
  let message = reminder.title;
  let title = kindLabel;
  if (offset > 0) title = `${kindLabel} in ${offset} min`;
  if (missed) title = `${kindLabel} (missed while Chrome was closed)`;
  const ctx = reminder.related_title ? reminder.related_title : reminder.description || '';
  const buttons = [];
  if (reminder.related_url) buttons.push({ title: 'Open page' });
  else buttons.push({ title: 'Mark done' });
  buttons.push({ title: 'Snooze 10 min' });
  return {
    type: 'basic',
    title,
    message,
    contextMessage: missed ? `Was due ${formatTime(fireTime(reminder), now)}` : ctx.slice(0, 120),
    buttons,
    priority: reminder.kind === 'reminder' ? 1 : 2,
    requireInteraction: requireInteraction && (reminder.kind !== 'reminder' || offset === 0),
  };
}

export function notificationId(reminderId, offset) {
  return `nova-rem:${reminderId}:${offset}:${Date.now()}`;
}

export function parseNotificationId(id) {
  const m = /^nova-rem:(\d+):(\d+):/.exec(id || '');
  return m ? { id: Number(m[1]), offset: Number(m[2]) } : null;
}

/** Keep the delivered-set small: drop entries for reminders that are gone or rescheduled. */
export function pruneDelivered(delivered, reminders, nowMs) {
  const keep = new Set();
  const live = new Map((reminders || []).map((r) => [r.id, fireTime(r)]));
  for (const name of delivered) {
    const p = parseAlarmName(name);
    if (!p) continue;
    if (live.get(p.id) === p.fireAt || nowMs - p.fireAt < MISSED_WINDOW_MS * 2) keep.add(name);
  }
  return keep;
}

/** Apply a snooze locally (offline-first) so alarms are correct even before the server confirms. */
export function localSnooze(reminders, id, minutes, nowMs) {
  return reminders.map((r) =>
    r.id === id
      ? { ...r, status: 'snoozed', snoozed_until: new Date(nowMs + minutes * 60000).toISOString(), next_fire_at: new Date(nowMs + minutes * 60000).toISOString() }
      : r,
  );
}

export function isRestrictedUrl(url) {
  if (!url) return true;
  if (!/^https?:\/\//i.test(url)) return true;
  return /^https?:\/\/(chrome\.google\.com\/webstore|chromewebstore\.google\.com)/i.test(url);
}
