// NOVA's voice pipeline, driven from the service worker. Four separate stages:
//
//   capture      — offscreen document (src/offscreen): mic, "Hey NOVA" wake word, endpointing
//   recognition  — the NOVA server (/api/voice/transcribe): Gemini transcription, Groq fallback
//   reasoning    — the NOVA server (/api/assistant/message): LangGraph agents + tools
//   speech       — the NOVA server (/api/voice/speak): Gemini TTS, played in the offscreen
//                  document; Chrome's built-in voice is the fallback
//
// Two ways in:
//   * Hands-free: "Hey NOVA, …" → the exchange runs here and is mirrored on the orb of the tab
//     the user is looking at. NOVA keeps listening for follow-ups ("make it 6 instead") until the
//     user stops talking, says "that's all", or the conversation-mode setting is off.
//   * Push-to-talk from the orb / dashboard (nova:dictate): the clip is transcribed and the text
//     is returned to that UI, which shows it (editable) and runs the request itself.
//
// Saying "Hey NOVA" while NOVA is speaking interrupts it (barge-in); so does the stop button.
import { getState, getSettings } from '../lib/storage.js';
import { expectsAnswer, isDismissal, isThanks, stripWake } from '../lib/wake.js';
import { isRestrictedUrl } from '../lib/scheduler.js';

const OFFSCREEN = 'src/offscreen/offscreen.html';
const ICON = chrome.runtime.getURL('icons/icon128.png');
const MAX_TURNS = 8; // follow-up turns in one hands-free conversation

const ERRORS = {
  mic_denied: 'Microphone access is blocked. Open NOVA settings → Voice → Grant access.',
  no_mic: 'No microphone was found.',
  mic_lost: 'The microphone was disconnected.',
  model_failed: 'The wake-word model could not be loaded. Run "npm run setup" in the extension folder and reload NOVA.',
};

export function transcribeErrorMessage(e) {
  if (e?.offline) return 'I can’t reach the NOVA server right now. Check that it’s running and try again.';
  if (e?.status === 503) return 'Voice needs GEMINI_API_KEY (or GROQ_API_KEY) on the NOVA server. You can type instead.';
  if (e?.status === 401) return 'NOVA is not connected to your account.';
  if (e?.status === 429) return 'Too many voice requests just now — wait a moment and try again.';
  return 'I couldn’t understand that audio. Please try again, or type.';
}

export function createVoiceLoop({ ask, pageContext, transcribe, synthesize, sendToTab, refreshBadge }) {
  const V = {
    tabId: null, busy: false, turns: 0, gen: 0,
    dictation: null, // { target, seq } while a push-to-talk clip is being recorded/transcribed
    captureSeq: 0, finalSeq: 0, previewBusy: false,
    speakSeq: 0, speakingId: 0, speakTarget: null,
  };
  const playbackWaiters = new Map(); // playback id → resolve(outcome)
  let creating = null;

  // ---------------------------------------------------------------- offscreen voice engine
  async function hasListener() {
    const ctx = await chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'], documentUrls: [chrome.runtime.getURL(OFFSCREEN)] });
    return ctx.length > 0;
  }

  async function ensureListener() {
    if (await hasListener()) return;
    creating ??= chrome.offscreen
      .createDocument({ url: OFFSCREEN, reasons: ['USER_MEDIA'], justification: 'Records voice commands, listens for "Hey NOVA" and plays NOVA’s voice.' })
      .finally(() => (creating = null));
    await creating;
  }

  async function toListener(type, extra = {}) {
    await ensureListener();
    for (let i = 0; i < 20; i++) {
      try {
        return await chrome.runtime.sendMessage({ target: 'offscreen', type, ...extra });
      } catch {
        await new Promise((r) => setTimeout(r, 100)); // document still loading its scripts
      }
    }
    return null;
  }

  async function setStatus(status) {
    await chrome.storage.session.set({ wakeStatus: { ...status, at: Date.now() } });
  }

  async function wakeWanted() {
    const st = await getState();
    return !!(st.token && st.settings.voice.enabled && st.settings.voice.wakeWord);
  }

  async function sync() {
    if (await wakeWanted()) return toListener('wake:start');
    if (await hasListener()) await toListener('wake:stop'); // keeps the engine for push-to-talk/playback
    await setStatus({ state: 'off' });
    return { mode: 'off' };
  }

  // ---------------------------------------------------------------- where UI updates go
  // target: { tabId } → the orb on that tab; { runtime: true } → extension pages (dashboard).
  function emit(target, msg) {
    if (!target) return Promise.resolve();
    if (target.tabId !== undefined && target.tabId !== null) return sendToTab(target.tabId, msg, false).catch(() => {});
    return chrome.runtime.sendMessage(msg).catch(() => {});
  }
  const targetOf = (sender) => (sender?.tab?.id !== undefined ? { tabId: sender.tab.id } : { runtime: true });

  function toTab(payload) {
    if (V.tabId === null) return Promise.resolve();
    return emit({ tabId: V.tabId }, { type: 'nova:voice', ...payload });
  }

  // The web page the user is looking at: the active tab of the focused window, or else the most
  // recently used active tab of any window, provided NOVA's orb script answers there. (Tab URLs
  // aren't visible without the broad "tabs" permission, so we ask the page instead.) No such tab
  // (new tab page, chrome://…, Web Store) → replies go to notifications.
  async function pickTab() {
    const [focused] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    const others = (await chrome.tabs.query({ active: true })).sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
    const seen = new Set();
    for (const t of [focused, ...others]) {
      if (!t || seen.has(t.id) || (t.url && isRestrictedUrl(t.url))) continue;
      seen.add(t.id);
      try {
        if ((await chrome.tabs.sendMessage(t.id, { type: 'nova:ping' }))?.ok) return t.id;
      } catch {
        /* no orb script on this tab */
      }
    }
    return null;
  }

  async function badge(on) {
    await chrome.action.setBadgeBackgroundColor({ color: on ? '#0E7490' : '#6E56FF' });
    if (on) await chrome.action.setBadgeText({ text: '🎙' });
    else await refreshBadge().catch(() => chrome.action.setBadgeText({ text: '' }));
  }

  // ---------------------------------------------------------------- speech output
  function stopSpeaking() {
    V.speakingId = 0; // also cancels a reply that is still being synthesized
    chrome.tts.stop();
    for (const resolve of playbackWaiters.values()) resolve('interrupted');
    playbackWaiters.clear();
    return hasListener().then((has) => (has ? toListener('voice:stopAudio') : null));
  }

  function browserSpeak(text, settings, onStart) {
    return new Promise(async (resolve) => {
      const voices = await chrome.tts.getVoices();
      const opts = { rate: settings.voice.rate || 1, enqueue: false };
      if (settings.voice.voiceName && voices.some((v) => v.voiceName === settings.voice.voiceName)) opts.voiceName = settings.voice.voiceName;
      if (settings.voice.lang) opts.lang = settings.voice.lang;
      const timer = setTimeout(() => resolve('timeout'), 60000);
      chrome.tts.speak(String(text).replace(/[•“”"*_#]/g, '').replace(/\p{Extended_Pictographic}/gu, '').slice(0, 600), {
        ...opts,
        onEvent: (e) => {
          if (e.type === 'start') onStart();
          if (['end', 'interrupted', 'cancelled', 'error'].includes(e.type)) {
            clearTimeout(timer);
            resolve(e.type === 'end' ? 'end' : 'interrupted');
          }
        },
      });
    });
  }

  // Speaks `text` and resolves when finished: 'end' | 'interrupted' | 'skipped'.
  // `onState(state, extra)` receives 'start' (with engine) and 'level' updates for animations.
  async function speak(text, target, onState = () => {}) {
    const settings = await getSettings();
    if (!settings.voice.tts || !text) return 'skipped';
    await stopSpeaking(); // never two replies at once
    const id = ++V.speakSeq;
    V.speakingId = id;
    V.speakTarget = { target, onState };
    if (settings.voice.engine !== 'browser') {
      try {
        const r = await synthesize({ text, voice: settings.voice.geminiVoice });
        if (V.speakingId !== id) return 'interrupted'; // stopped or barged in while synthesizing
        const done = new Promise((resolve) => playbackWaiters.set(id, resolve));
        const res = await toListener('voice:play', { id, audio: r.audio });
        if (!res || res.error) throw new Error(res?.error || 'no voice engine');
        const outcome = await Promise.race([done, new Promise((r2) => setTimeout(() => r2('timeout'), 180000))]);
        playbackWaiters.delete(id);
        if (outcome !== 'error') return outcome;
      } catch (e) {
        playbackWaiters.delete(id);
        if (V.speakingId !== id) return 'interrupted';
        console.info('NOVA: natural voice unavailable, using the browser voice.', e?.message || e);
      }
    }
    if (V.speakingId !== id) return 'interrupted';
    return browserSpeak(text, settings, () => onState('start', { engine: 'browser' }));
  }

  function notify(title, message) {
    return chrome.notifications.create(`nova-voice:${Date.now()}`, { type: 'basic', iconUrl: ICON, title, message: message.slice(0, 300), priority: 0 });
  }

  // ---------------------------------------------------------------- hands-free exchange
  async function finish({ followUp = false } = {}) {
    if (followUp) {
      V.turns++;
      await toTab({ phase: 'listening', followUp: true });
      await toListener('wake:listen', { answer: true });
      return;
    }
    V.busy = false;
    V.turns = 0;
    await toTab({ phase: 'done' });
    await badge(false);
    await toListener('wake:resume');
  }

  async function onWake() {
    V.gen++; // a new exchange: anything still running from the previous one is abandoned
    await stopSpeaking();
    if (V.dictation) V.dictation = null;
    V.busy = true;
    V.turns = 0;
    V.tabId = await pickTab();
    await badge(true);
    await toTab({ phase: 'listening' });
  }

  async function onNoSpeech({ purpose }) {
    if (purpose === 'dictate') {
      const d = V.dictation;
      V.dictation = null;
      return emit(d?.target, { type: 'nova:dictation', phase: 'nospeech' });
    }
    if (V.turns > 0) return finish(); // conversation simply ends when the user stops talking
    await toTab({ phase: 'error', message: 'I didn’t hear a command.' });
    return finish();
  }

  async function onCommand({ audio, mime, purpose }) {
    if (purpose === 'dictate') return onDictation({ audio, mime });
    const gen = V.gen;
    const stale = () => gen !== V.gen;
    V.finalSeq = ++V.captureSeq;
    await toTab({ phase: 'thinking' });
    let text = '';
    try {
      const t = await transcribe({ audio, mime });
      text = stripWake(t.text);
    } catch (e) {
      if (stale()) return;
      const msg = transcribeErrorMessage(e);
      await toTab({ phase: 'error', message: msg });
      if (V.tabId === null) await notify('NOVA', msg);
      await speak(msg, null);
      return finish();
    }
    if (stale()) return;
    if (!text || /^[\s.,!?]*$/.test(text)) {
      if (V.turns > 0) return finish(); // no answer to a follow-up → just stop listening
      await toTab({ phase: 'error', message: 'Sorry, I didn’t catch that.' });
      await speak('Sorry, I didn’t catch that. Say “Hey NOVA” to try again.', null);
      return finish();
    }
    await toTab({ phase: 'heard', text });
    if (isDismissal(text)) {
      await speak('Okay.', null);
      return finish();
    }
    if (isThanks(text) && V.turns > 0) {
      await toTab({ phase: 'result', text, data: { reply: 'You’re welcome!', status: 'done' } });
      await speak('You’re welcome!', null);
      return finish();
    }

    let res;
    try {
      const tabId = V.tabId ?? undefined;
      const { page } = await pageContext({ tabId });
      res = await ask({ message: text, page, channel: 'voice' });
      if (res.status === 'needs_permission' && page?.url) {
        const settings = await getSettings();
        const host = new URL(page.url).hostname;
        if (settings.pageAccess === 'always' || settings.pageAccessSites.includes(host)) {
          const withText = await pageContext({ withText: true, tabId });
          res = await ask({ message: text, page: withText.page, pageTextAllowed: true, channel: 'voice' });
        }
      }
    } catch (e) {
      if (stale()) return;
      const msg = e.offline ? 'I can’t reach the NOVA server. Check your connection and that the server is running.' : e.message || 'Something went wrong.';
      await toTab({ phase: 'error', message: msg });
      await speak(msg, null);
      return finish();
    }
    if (stale()) return;

    await toTab({ phase: 'result', text, data: res });
    if (V.tabId === null) await notify(`NOVA — “${text.slice(0, 60)}”`, res.reply || '');
    // Keep the wake word armed while speaking so "Hey NOVA" can interrupt (barge-in).
    if (await wakeWanted()) await toListener('wake:resume');
    const outcome = await speak(res.reply, { tabId: V.tabId }, (state, extra) => {
      if (state === 'start') toTab({ phase: 'speaking' });
      else if (state === 'level') toTab({ phase: 'level', level: extra.level });
    });
    if (stale() || outcome === 'interrupted') {
      if (!stale()) await finish(); // stopped with the button → end the conversation
      return;
    }
    const settings = await getSettings();
    const needsAnswer = expectsAnswer(res); // incl. "Would you like to connect your Google Calendar?"
    const keepTalking = settings.voice.conversation && res.status !== 'error' && res.status !== 'needs_authorization';
    return finish({ followUp: (needsAnswer || keepTalking) && V.turns < MAX_TURNS });
  }

  // ---------------------------------------------------------------- push-to-talk (orb / dashboard)
  async function startDictation(target) {
    if (V.busy) return { ok: false, error: 'busy', message: 'NOVA is in a hands-free conversation right now.' };
    await stopSpeaking();
    const res = await toListener('voice:capture');
    if (!res) return { ok: false, error: 'engine_failed', message: 'The voice engine didn’t start. Reload NOVA in chrome://extensions.' };
    if (res.error) return { ok: false, error: res.error, message: ERRORS[res.error] || `Microphone error (${res.error}).` };
    V.dictation = { target, seq: ++V.captureSeq };
    return { ok: true };
  }

  async function onDictation({ audio, mime }) {
    const d = V.dictation;
    V.dictation = null;
    if (!d) return;
    V.finalSeq = d.seq;
    await emit(d.target, { type: 'nova:dictation', phase: 'transcribing' });
    try {
      const t = await transcribe({ audio, mime });
      const text = stripWake(t.text || '');
      await emit(d.target, { type: 'nova:dictation', phase: text ? 'final' : 'nospeech', text, provider: t.provider });
    } catch (e) {
      await emit(d.target, { type: 'nova:dictation', phase: 'error', message: transcribeErrorMessage(e), offline: !!e.offline });
    }
  }

  // Live transcript while the user is still talking (best effort; dropped if the final arrives first).
  async function onPreview({ audio, mime, purpose }) {
    if (V.previewBusy) return;
    const seq = purpose === 'dictate' ? V.dictation?.seq : V.captureSeq;
    const target = purpose === 'dictate' ? V.dictation?.target : null;
    if (purpose === 'dictate' && !target) return;
    V.previewBusy = true;
    try {
      const t = await transcribe({ audio, mime, preview: true });
      const text = stripWake(t.text || '');
      if (!text || V.finalSeq >= seq) return;
      if (purpose === 'dictate') await emit(target, { type: 'nova:dictation', phase: 'partial', text });
      else await toTab({ phase: 'partial', text });
    } catch {
      /* previews are optional */
    } finally {
      V.previewBusy = false;
    }
  }

  // Never leave the listener paused because of an unexpected error.
  const guarded = (fn) => async (msg) => {
    try {
      return await fn(msg);
    } catch (e) {
      console.warn('NOVA voice loop error', e);
      V.busy = false;
      await toTab({ phase: 'done' });
      await badge(false);
      await toListener('wake:resume');
      return null;
    }
  };

  const handlers = {
    'nova:wakeStatus': async (m) => {
      const status = { state: m.state, mode: m.mode, error: m.error || null, message: m.error ? ERRORS[m.error] || `Voice listener error (${m.error}).` : null };
      if (m.wakeWanted === false && m.state !== 'error') return status; // push-to-talk only; not a wake-word status
      await setStatus(status);
      return status;
    },
    'nova:wake': guarded(onWake),
    'nova:voiceLevel': (m) => {
      if (m.purpose === 'dictate') emit(V.dictation?.target, { type: 'nova:dictation', phase: 'level', level: m.level });
      else toTab({ phase: 'level', level: m.level });
      return null;
    },
    'nova:voicePreview': (m) => {
      onPreview(m);
      return null;
    },
    'nova:voiceNoSpeech': guarded(onNoSpeech),
    'nova:voiceCommand': guarded(onCommand),
    'nova:voiceError': async (m) => {
      const d = V.dictation;
      V.dictation = null;
      await emit(d?.target, { type: 'nova:dictation', phase: 'error', message: ERRORS[m.error] || 'Microphone error.' });
      return null;
    },
    'nova:playback': (m) => {
      const s = V.speakTarget;
      if (m.state === 'start') s?.onState('start', { engine: 'gemini', seconds: m.seconds });
      if (m.state === 'end' || m.state === 'error') {
        playbackWaiters.get(m.id)?.(m.state === 'error' ? 'error' : m.interrupted ? 'interrupted' : 'end');
        playbackWaiters.delete(m.id);
      }
      return null;
    },
    'nova:speakLevel': (m) => {
      V.speakTarget?.onState('level', { level: m.level });
      return null;
    },
    'nova:bargeIn': () => {
      for (const resolve of playbackWaiters.values()) resolve('interrupted');
      playbackWaiters.clear();
      return null;
    },
    'nova:voiceCancel': async () => {
      V.gen++;
      await stopSpeaking();
      await toListener('wake:cancel');
      return finish();
    },
    'nova:ttsStop': async () => {
      await stopSpeaking();
      return { ok: true };
    },
    // Push-to-talk from the orb (content script) or the dashboard.
    'nova:dictate': (m, sender) => startDictation(targetOf(sender)),
    'nova:dictateStop': async () => (V.dictation ? toListener('voice:finish') : null),
    'nova:dictateCancel': async () => {
      V.dictation = null;
      return toListener('wake:cancel');
    },
    // Speak a reply for a UI-driven exchange; the UI gets nova:speech start/level/end events.
    'nova:speak': async (m, sender) => {
      const target = targetOf(sender);
      const outcome = await speak(m.text, target, (state, extra) => emit(target, { type: 'nova:speech', state, ...extra }));
      await emit(target, { type: 'nova:speech', state: 'end', outcome });
      return { outcome };
    },
    'nova:wakeSync': () => sync(),
    'nova:wakeHold': async (m) => (V.busy ? null : toListener(m.hold ? 'wake:pause' : 'wake:resume')),
  };

  return { sync, handlers, state: V, hasListener, toListener, speak, stopSpeaking };
}
