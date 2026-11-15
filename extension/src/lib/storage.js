// Persistent extension state (chrome.storage.local). The API token lives here — never
// AI provider keys, which exist only on the backend.

export const DEFAULT_SETTINGS = Object.freeze({
  orbEnabled: true,
  animations: true,
  suggestions: true,
  pageAccess: 'ask', // 'ask' | 'always' | 'never'  (reading page *text*; URL/title are always used)
  theme: 'auto', // 'auto' | 'dark' | 'light'
  // wakeWord: hands-free "Hey NOVA". engine: 'gemini' (natural voice from the server) | 'browser'
  // (Chrome's built-in voices, also the automatic fallback). conversation: keep listening for
  // follow-ups after NOVA answers in hands-free mode. voiceName/rate apply to the browser voice.
  voice: { enabled: true, wakeWord: false, lang: '', tts: true, engine: 'gemini', geminiVoice: 'Kore', conversation: true, voiceName: '', rate: 1 },
  notifications: { requireInteraction: true },
  disabledSites: [],
  pageAccessSites: [],
  orbPosition: null,
});

export const DEFAULT_STATE = Object.freeze({
  backendUrl: 'http://localhost:8000',
  token: null,
  user: null,
  sessionId: null,
});

function merge(base, over) {
  if (!over || typeof over !== 'object' || Array.isArray(over)) return over ?? base;
  const out = { ...base };
  for (const [k, v] of Object.entries(over)) {
    out[k] = base && typeof base[k] === 'object' && !Array.isArray(base[k]) && base[k] !== null ? merge(base[k], v) : v;
  }
  return out;
}

export function mergeSettings(stored) {
  return merge(structuredClone(DEFAULT_SETTINGS), stored || {});
}

export async function getState() {
  const s = await chrome.storage.local.get(['backendUrl', 'token', 'user', 'settings', 'sessionId']);
  return {
    backendUrl: (s.backendUrl || DEFAULT_STATE.backendUrl).replace(/\/+$/, ''),
    token: s.token || null,
    user: s.user || null,
    sessionId: s.sessionId || null,
    settings: mergeSettings(s.settings),
  };
}

export async function getSettings() {
  return (await getState()).settings;
}

export async function updateSettings(patch) {
  const current = await getSettings();
  const next = merge(current, patch);
  await chrome.storage.local.set({ settings: next });
  return next;
}

export async function getSessionId() {
  let { sessionId } = await chrome.storage.local.get('sessionId');
  if (!sessionId) {
    sessionId = 's' + crypto.randomUUID().replace(/-/g, '').slice(0, 24);
    await chrome.storage.local.set({ sessionId });
  }
  return sessionId;
}

export async function newSession() {
  await chrome.storage.local.remove('sessionId');
  return getSessionId();
}
