import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  ALARM_PREFIX, alarmName, diffAlarms, isRestrictedUrl, localSnooze, notificationFor, parseAlarmName,
  parseNotificationId, planAlarms, pruneDelivered,
} from '../src/lib/scheduler.js';

const NOW = Date.parse('2026-11-07T08:30:00Z');
const at = (min) => new Date(NOW + min * 60000).toISOString();
const rem = (id, min, extra = {}) => ({ id, title: `R${id}`, kind: 'reminder', status: 'pending', scheduled_at: at(min), next_fire_at: at(min), notify_offsets: [0], ...extra });

test('alarm names round-trip', () => {
  const n = alarmName(12, 15, NOW);
  assert.ok(n.startsWith(ALARM_PREFIX));
  assert.deepEqual(parseAlarmName(n), { id: 12, offset: 15, fireAt: NOW });
  assert.equal(parseAlarmName('nova:sync'), null);
  assert.equal(parseAlarmName('nova:r:x:y:z'), null);
});

test('plans one alarm per future offset, sorted', () => {
  const { alarms, missed } = planAlarms([rem(2, 120), rem(1, 60, { kind: 'meeting', notify_offsets: [10, 0] })], NOW);
  assert.deepEqual(alarms.map((a) => [a.id, a.offset]), [[1, 10], [1, 0], [2, 0]]);
  assert.equal(alarms[0].when, NOW + 50 * 60000);
  assert.equal(missed.length, 0);
});

test('skips completed/cancelled/fired reminders', () => {
  const { alarms } = planAlarms([rem(1, 60, { status: 'completed' }), rem(2, 60, { status: 'cancelled' }), rem(3, 60, { status: 'fired' })], NOW);
  assert.equal(alarms.length, 0);
});

test('snoozed reminders use next_fire_at', () => {
  const r = rem(1, -30, { status: 'snoozed', next_fire_at: at(10), snoozed_until: at(10) });
  const { alarms } = planAlarms([r], NOW);
  assert.equal(alarms[0].when, NOW + 10 * 60000);
});

test('past-due reminders within 24h are reported as missed (TEST 7: restart while closed)', () => {
  const { alarms, missed } = planAlarms([rem(1, -90), rem(2, -60 * 30), rem(3, -5, { notify_offsets: [10, 0] })], NOW);
  assert.equal(alarms.length, 0);
  assert.deepEqual(missed.map((m) => m.reminder.id).sort(), [1, 3]); // R2 is >24h old; lead offsets are not "missed"
});

test('delivered notifications are never re-planned', () => {
  const r = rem(1, -1);
  const name = alarmName(1, 0, Date.parse(r.next_fire_at));
  assert.equal(planAlarms([r], NOW, new Set([name])).missed.length, 0);
});

test('diffAlarms only touches NOVA reminder alarms', () => {
  const plan = planAlarms([rem(1, 60), rem(2, 90)], NOW).alarms;
  const existing = [{ name: plan[0].name }, { name: `${ALARM_PREFIX}99:0:1` }, { name: 'nova:sync' }];
  const { create, clear } = diffAlarms(existing, plan);
  assert.deepEqual(create.map((a) => a.id), [2]);
  assert.deepEqual(clear, [`${ALARM_PREFIX}99:0:1`]);
});

test('rescheduling changes the alarm name so stale alarms get cleared', () => {
  const before = planAlarms([rem(1, 60)], NOW).alarms;
  const after = planAlarms([rem(1, 120)], NOW).alarms;
  const { create, clear } = diffAlarms(before.map((a) => ({ name: a.name })), after);
  assert.equal(create.length, 1);
  assert.deepEqual(clear, [before[0].name]);
});

test('notification content and buttons', () => {
  const withUrl = notificationFor(rem(1, 0, { related_url: 'https://youtube.com/watch?v=1', related_title: 'Video' }), 0);
  assert.equal(withUrl.buttons[0].title, 'Open page');
  assert.equal(withUrl.buttons[1].title, 'Snooze 10 min');
  assert.equal(withUrl.contextMessage, 'Video');
  const alarm = notificationFor(rem(2, 0, { kind: 'alarm' }), 0);
  assert.equal(alarm.buttons[0].title, 'Mark done');
  assert.equal(alarm.requireInteraction, true);
  assert.equal(alarm.priority, 2);
  const heads = notificationFor(rem(3, 0, { kind: 'meeting' }), 10);
  assert.match(heads.title, /in 10 min/);
  const missed = notificationFor(rem(4, -60), 0, { missed: true, now: NOW });
  assert.match(missed.title, /missed/);
  assert.match(missed.contextMessage, /Was due/);
});

test('notification ids parse', () => {
  assert.deepEqual(parseNotificationId('nova-rem:5:10:123'), { id: 5, offset: 10 });
  assert.equal(parseNotificationId('nova-test:1'), null);
});

test('localSnooze moves the next fire time (offline-first)', () => {
  const out = localSnooze([rem(1, -1), rem(2, 30)], 1, 10, NOW);
  assert.equal(out[0].status, 'snoozed');
  assert.equal(Date.parse(out[0].next_fire_at), NOW + 10 * 60000);
  assert.equal(out[1].status, 'pending');
});

test('pruneDelivered drops old entries', () => {
  const old = alarmName(1, 0, NOW - 3 * 86400000);
  const fresh = alarmName(2, 0, NOW - 3600000);
  const kept = pruneDelivered(new Set([old, fresh, 'garbage']), [], NOW);
  assert.deepEqual([...kept], [fresh]);
});

test('restricted URLs', () => {
  for (const u of ['chrome://extensions', 'chrome-extension://abc/x.html', 'about:blank', 'file:///C:/x.pdf', 'https://chromewebstore.google.com/detail/x', '', null]) assert.ok(isRestrictedUrl(u), u);
  assert.ok(!isRestrictedUrl('https://example.com'));
});
