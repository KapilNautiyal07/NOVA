import assert from 'node:assert/strict';
import { test } from 'node:test';

// Minimal chrome.storage mock
const store = {};
globalThis.chrome = {
  storage: {
    local: {
      get: async (keys) => Object.fromEntries([].concat(keys).filter((k) => k in store).map((k) => [k, structuredClone(store[k])])),
      set: async (obj) => Object.assign(store, structuredClone(obj)),
      remove: async (keys) => [].concat(keys).forEach((k) => delete store[k]),
    },
  },
};

const { getState, updateSettings, getSessionId, newSession, mergeSettings } = await import('../src/lib/storage.js');
const { qs } = await import('../src/lib/api.js');

test('defaults are applied', async () => {
  const s = await getState();
  assert.equal(s.backendUrl, 'http://localhost:8000');
  assert.equal(s.settings.pageAccess, 'ask');
  assert.equal(s.settings.voice.enabled, true);
  assert.equal(s.token, null);
});

test('deep-merges settings patches', async () => {
  await updateSettings({ voice: { tts: false } });
  const s = await getState();
  assert.equal(s.settings.voice.tts, false);
  assert.equal(s.settings.voice.enabled, true, 'sibling keys preserved');
  await updateSettings({ disabledSites: ['a.com'] });
  assert.deepEqual((await getState()).settings.disabledSites, ['a.com']);
});

test('mergeSettings tolerates garbage', () => {
  assert.equal(mergeSettings(null).theme, 'auto');
  assert.equal(mergeSettings({ theme: 'dark' }).theme, 'dark');
});

test('session ids are stable until reset', async () => {
  const a = await getSessionId();
  assert.match(a, /^s[a-f0-9]{24}$/);
  assert.equal(await getSessionId(), a);
  assert.notEqual(await newSession(), a);
});

test('query string helper repeats arrays and drops empties', () => {
  assert.equal(qs({ status: ['pending', 'snoozed'], q: '', limit: 5 }), '?status=pending&status=snoozed&limit=5');
  assert.equal(qs({}), '');
});
