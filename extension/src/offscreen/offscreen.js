// NOVA voice engine (offscreen document): the ONLY place that touches the microphone or plays
// NOVA's voice. One extension-wide mic permission (granted once on mic.html) instead of a prompt
// on every website, and one audio pipeline for every way of talking to NOVA.
//
// Capture modes:
//   off       — mic closed.
//   idle      — mic open but nothing is processed (between push-to-talk turns; closes soon).
//   wake      — "Hey NOVA" on: audio goes only to the on-device Vosk recognizer (tiny grammar).
//               Nothing leaves the computer in this mode.
//   tentative — Vosk's partial result said "hey nova": keep the audio (so a one-breath
//               "Hey NOVA, remind me…" isn't cut off) but stay silent until Vosk's final result
//               confirms the phrase with high confidence ("Hey Noah" is discarded silently).
//   command   — record until the user has clearly finished (pause-tolerant endpointing), then
//               hand a 16 kHz WAV to the service worker. While recording, a preview clip is sent
//               every couple of seconds so the user sees a live transcript.
//   paused    — while NOVA thinks, so it can't hear itself.
//
// Playback: Gemini TTS audio is played here. While NOVA speaks the wake listener stays armed, so
// saying "Hey NOVA" interrupts it (barge-in).
import { downsample, encodeWav, Endpointer, isWake, NoiseFloor, rms, verifyWake, WAKE_GRAMMAR } from '../lib/wake.js';

const MODEL_URL = chrome.runtime.getURL('models/vosk-model-small-en-us-0.15.tar.gz');
const PREROLL_MS = 1500; // audio kept from before the partial wake (catches the start of "hey")
const TENTATIVE_MAX_MS = 12000; // give up on an unconfirmed wake after this long
const PAUSE_WATCHDOG_MS = 90000; // never stay deaf if the service worker forgets to resume us
const IDLE_CLOSE_MS = 20000; // push-to-talk only: release the mic (and Chrome's mic icon) after this
const PREVIEW_EVERY_MS = 2200; // live-transcript cadence while the user is talking
const MAX_PREVIEWS = 6;
// Natural speech has pauses ("remind me to call… the electricity office"). After the voice drops,
// wait 1.3 s before deciding the user is done — or 2 s while they've said very little, which is
// when people stop to think. (Tuned on recorded clips: the old 1.1 s and a flat 1.5 s both cut a
// 1.6 s mid-sentence pause; this doesn't.) Short replies to a question ("yes", "10 AM") end sooner.
const COMMAND_ENDPOINT = { silenceMs: 1300, shortSpeechMs: 1300, hesitationMs: 2000, noSpeechMs: 8000, maxMs: 30000, minSpeechMs: 250 };
const ANSWER_ENDPOINT = { silenceMs: 1200, noSpeechMs: 7000, maxMs: 20000, minSpeechMs: 200 };

const S = {
  mode: 'off',
  wakeWanted: false,
  model: null,
  recognizer: null,
  stream: null,
  ctx: null,
  node: null,
  rate: 48000,
  floor: new NoiseFloor(),
  ring: [], // recent frames for the pre-roll
  ringMs: 0,
  clip: [], // frames of the current command
  purpose: 'command', // 'command' (hands-free / follow-up) | 'dictate' (push-to-talk from a UI)
  endpointer: null,
  endVerdict: null, // endpoint reached while still tentative ('done' | 'max')
  tentativeAt: 0,
  finalRequested: false,
  lastLevelSent: 0,
  lastPreviewAt: 0,
  previews: 0,
  wakeArmedAt: 0,
  watchdog: null,
  idleTimer: null,
  starting: null,
  // playback
  playCtx: null,
  playing: null, // { id, source, analyser, raf }
};

const send = (msg) => chrome.runtime.sendMessage(msg).catch(() => {});
const status = (state, extra = {}) => send({ type: 'nova:wakeStatus', state, wakeWanted: S.wakeWanted, ...extra });

// ------------------------------------------------------------------ setup
async function loadModel() {
  if (S.model) return S.model;
  status('loading');
  S.model = await Vosk.createModel(MODEL_URL, -1); // -1: errors only
  return S.model;
}

function newRecognizer() {
  if (!S.model) return;
  try {
    S.recognizer?.remove();
  } catch {
    /* already gone */
  }
  const rec = new S.model.KaldiRecognizer(S.rate, WAKE_GRAMMAR);
  rec.setWords(true); // per-word confidences for the final-result check
  rec.on('partialresult', (m) => isWake(m.result?.partial) && onPartialWake());
  rec.on('result', (m) => onFinal(m.result || {}));
  S.recognizer = rec;
}

// Opens the microphone. Returns an error code string on failure.
async function openMic() {
  if (S.stream) return null;
  try {
    S.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    return e.name === 'NotAllowedError' ? 'mic_denied' : e.name === 'NotFoundError' ? 'no_mic' : `mic_${e.name}`;
  }
  S.ctx = new AudioContext();
  S.rate = S.ctx.sampleRate;
  const source = S.ctx.createMediaStreamSource(S.stream);
  // ScriptProcessorNode is deprecated but works in every Chrome and needs no extra module file.
  S.node = S.ctx.createScriptProcessor(4096, 1, 1);
  S.node.onaudioprocess = (e) => onFrame(e.inputBuffer.getChannelData(0));
  const mute = S.ctx.createGain();
  mute.gain.value = 0; // the processor must reach the destination to run; keep it silent
  source.connect(S.node);
  S.node.connect(mute).connect(S.ctx.destination);
  S.stream.getAudioTracks()[0].addEventListener('ended', () => {
    closeMic();
    status('error', { error: 'mic_lost' });
    if (S.purpose === 'dictate') send({ type: 'nova:voiceError', purpose: 'dictate', error: 'mic_lost' });
  });
  return null;
}

function closeMic() {
  clearTimeout(S.watchdog);
  clearTimeout(S.idleTimer);
  S.node?.disconnect();
  S.stream?.getTracks().forEach((t) => t.stop());
  S.ctx?.close().catch(() => {});
  try {
    S.recognizer?.remove();
  } catch {
    /* ignore */
  }
  Object.assign(S, { node: null, stream: null, ctx: null, recognizer: null, ring: [], ringMs: 0, clip: [] });
  setMode('off');
}

// Turn the "Hey NOVA" listener on.
async function startWake() {
  S.wakeWanted = true;
  if (S.mode !== 'off' && S.mode !== 'idle' && S.recognizer) return;
  if (S.starting) return S.starting;
  S.starting = (async () => {
    // Model first, so the mic is only opened (and Chrome's mic indicator shown) once we can listen.
    try {
      await loadModel();
    } catch (e) {
      status('error', { error: 'model_failed', detail: String(e?.message || e || '') });
      return;
    }
    const err = await openMic();
    if (err) return status('error', { error: err });
    newRecognizer();
    if (S.mode === 'off' || S.mode === 'idle') setMode('wake');
  })();
  try {
    await S.starting;
  } finally {
    S.starting = null;
  }
}

function stopWake() {
  S.wakeWanted = false;
  if (S.mode === 'wake' || S.mode === 'tentative' || S.mode === 'paused' || S.mode === 'idle') closeMic();
  else status('off'); // a push-to-talk recording in progress finishes first, then the mic closes
}

function setMode(mode) {
  clearTimeout(S.watchdog);
  clearTimeout(S.idleTimer);
  const prev = S.mode;
  S.mode = mode;
  if (mode === 'wake') S.wakeArmedAt = performance.now();
  if (mode === 'paused') S.watchdog = setTimeout(() => S.mode === 'paused' && resumeListening(), PAUSE_WATCHDOG_MS);
  if (mode === 'idle') S.idleTimer = setTimeout(() => S.mode === 'idle' && closeMic(), IDLE_CLOSE_MS);
  if (!S.wakeWanted && mode !== 'off') return; // the wake status line only describes "Hey NOVA"
  // 'tentative' is internal: from the outside NOVA is still just waiting for its name.
  const visible = (m) => (m === 'tentative' ? 'wake' : m === 'idle' ? 'off' : m);
  if (visible(prev) !== visible(mode) || prev === 'off' || mode === 'off') status(visible(mode) === 'off' ? 'off' : 'on', { mode: visible(mode) });
}

// Back to the resting state: listening for "Hey NOVA" if it's on, otherwise idle (mic closes soon).
function resumeListening() {
  if (S.mode === 'off') return;
  S.ring = [];
  S.ringMs = 0;
  S.clip = [];
  S.endVerdict = null;
  if (S.wakeWanted && S.model) {
    newRecognizer(); // drop whatever the old recognizer had buffered
    setMode('wake');
  } else setMode('idle');
}

// ------------------------------------------------------------------ audio in
function onFrame(input) {
  const frame = new Float32Array(input); // the input buffer is reused by the audio thread
  const ms = (frame.length / S.rate) * 1000;
  const level = rms(frame);
  const now = performance.now();

  if (S.mode === 'wake' || S.mode === 'idle') {
    S.floor.update(level);
    S.ring.push(frame);
    S.ringMs += ms;
    while (S.ringMs - (S.ring[0].length / S.rate) * 1000 > PREROLL_MS) S.ringMs -= (S.ring.shift().length / S.rate) * 1000;
    if (S.mode === 'wake') S.recognizer?.acceptWaveformFloat(frame, S.rate);
    return;
  }

  if (S.mode === 'tentative') {
    S.recognizer?.acceptWaveformFloat(frame, S.rate); // keep feeding until the final result arrives
    S.clip.push(frame);
    const verdict = S.endpointer.push(level, now);
    if ((verdict === 'done' || verdict === 'max' || verdict === 'no_speech') && !S.finalRequested) {
      S.endVerdict = verdict === 'no_speech' ? 'done' : verdict;
      S.finalRequested = true;
      S.recognizer?.retrieveFinalResult(); // user stopped talking: ask Vosk to decide now
    }
    if (now - S.tentativeAt > TENTATIVE_MAX_MS) {
      if (!S.finalRequested) {
        S.finalRequested = true;
        S.recognizer?.retrieveFinalResult();
      } else if (now - S.tentativeAt > TENTATIVE_MAX_MS + 2000) resumeListening(); // never confirmed
    }
    return;
  }

  if (S.mode === 'command') {
    S.clip.push(frame);
    const verdict = S.endpointer.push(level, now);
    if (now - S.lastLevelSent > 90) {
      S.lastLevelSent = now;
      send({ type: 'nova:voiceLevel', purpose: S.purpose, level: Math.min(1, level / Math.max(S.endpointer.threshold() * 4, 0.05)) });
    }
    if (verdict === 'speaking' && S.previews < MAX_PREVIEWS && now - S.lastPreviewAt > PREVIEW_EVERY_MS) {
      S.lastPreviewAt = now;
      S.previews++;
      send({ type: 'nova:voicePreview', purpose: S.purpose, audio: wavBase64(S.clip), mime: 'audio/wav' });
    }
    if (verdict === 'done' || verdict === 'max') finishCommand(verdict);
    else if (verdict === 'no_speech') {
      const purpose = S.purpose;
      S.clip = [];
      setMode(S.wakeWanted ? 'paused' : 'idle');
      send({ type: 'nova:voiceNoSpeech', purpose });
    }
  }
}

// Stage 1: fast partial result. Start keeping audio, tell nobody yet.
function onPartialWake() {
  if (S.mode !== 'wake' || performance.now() - S.wakeArmedAt < 800) return;
  S.clip = [...S.ring];
  S.ring = [];
  S.ringMs = 0;
  S.endpointer = new Endpointer({ floor: S.floor.value, ...COMMAND_ENDPOINT, noSpeechMs: 7000 });
  S.endpointer.reset(performance.now());
  S.endVerdict = null;
  S.finalRequested = false;
  S.tentativeAt = performance.now();
  S.purpose = 'command';
  setMode('tentative');
}

// Stage 2: Vosk's final result for the utterance.
function onFinal(result) {
  const confirmed = verifyWake(result.result);
  if (S.mode === 'wake') {
    // Rare: final arrived without a partial wake first. Start from the pre-roll.
    if (!confirmed || performance.now() - S.wakeArmedAt < 800) return;
    onPartialWake();
  }
  if (S.mode !== 'tentative') return;
  if (!confirmed) return resumeListening(); // near miss: discard silently
  stopPlayback(true); // barge-in: "Hey NOVA" while NOVA is talking interrupts it
  chime();
  send({ type: 'nova:wake' });
  S.lastPreviewAt = performance.now();
  S.previews = 0;
  setMode('command');
  if (S.endVerdict) finishCommand(S.endVerdict); // the whole command was already said in one breath
}

function beginCommand(purpose, endpoint = COMMAND_ENDPOINT) {
  S.purpose = purpose;
  // Keep ~0.5 s from before the button press/chime so a quick starter isn't clipped.
  const keepMs = 500;
  let kept = 0;
  const pre = [];
  for (let i = S.ring.length - 1; i >= 0 && kept < keepMs; i--) {
    pre.unshift(S.ring[i]);
    kept += (S.ring[i].length / S.rate) * 1000;
  }
  S.clip = purpose === 'dictate' ? pre : [];
  S.ring = [];
  S.ringMs = 0;
  S.endpointer = new Endpointer({ floor: S.floor.value, ...endpoint });
  S.endpointer.reset(performance.now());
  S.lastPreviewAt = performance.now();
  S.previews = 0;
  setMode('command');
}

function wavBase64(frames) {
  const total = frames.reduce((n, f) => n + f.length, 0);
  const all = new Float32Array(total);
  let o = 0;
  for (const f of frames) {
    all.set(f, o);
    o += f.length;
  }
  const wav = encodeWav(downsample(all, S.rate, 16000), Math.min(16000, S.rate));
  let bin = '';
  for (let i = 0; i < wav.length; i += 0x8000) bin += String.fromCharCode.apply(null, wav.subarray(i, i + 0x8000));
  return btoa(bin);
}

function finishCommand(reason) {
  const frames = S.clip;
  const purpose = S.purpose;
  S.clip = [];
  S.endVerdict = null;
  setMode(S.wakeWanted ? 'paused' : 'idle');
  const total = frames.reduce((n, f) => n + f.length, 0);
  send({ type: 'nova:voiceCommand', purpose, audio: wavBase64(frames), mime: 'audio/wav', reason, seconds: Math.round(total / S.rate) });
}

// Short rising two-note chime so you know NOVA is listening.
function chime() {
  const ctx = S.ctx || ensurePlayCtx();
  const t = ctx.currentTime;
  for (const [i, f] of [660, 990].entries()) {
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = f;
    g.gain.setValueAtTime(0, t + i * 0.09);
    g.gain.linearRampToValueAtTime(0.18, t + i * 0.09 + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.09 + 0.16);
    osc.connect(g).connect(ctx.destination);
    osc.start(t + i * 0.09);
    osc.stop(t + i * 0.09 + 0.18);
  }
}

// ------------------------------------------------------------------ audio out (NOVA's voice)
function ensurePlayCtx() {
  if (!S.playCtx || S.playCtx.state === 'closed') S.playCtx = new AudioContext();
  return S.playCtx;
}

async function play(id, b64) {
  stopPlayback(false); // never two voices at once
  const ctx = ensurePlayCtx();
  if (ctx.state === 'suspended') await ctx.resume().catch(() => {});
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  let buffer;
  try {
    buffer = await ctx.decodeAudioData(bytes.buffer);
  } catch (e) {
    send({ type: 'nova:playback', id, state: 'error', error: 'decode_failed' });
    return;
  }
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser).connect(ctx.destination);
  const p = { id, source, analyser, raf: null, stopped: false };
  S.playing = p;
  const data = new Float32Array(analyser.fftSize);
  let last = 0;
  const tick = () => {
    if (S.playing !== p) return;
    const now = performance.now();
    if (now - last > 100) {
      last = now;
      analyser.getFloatTimeDomainData(data);
      send({ type: 'nova:speakLevel', id, level: Math.min(1, rms(data) * 6) });
    }
    p.raf = setTimeout(tick, 50);
  };
  source.onended = () => {
    clearTimeout(p.raf);
    if (S.playing === p) S.playing = null;
    send({ type: 'nova:playback', id, state: 'end', interrupted: p.stopped });
  };
  source.start();
  send({ type: 'nova:playback', id, state: 'start', seconds: buffer.duration });
  tick();
}

function stopPlayback(bargeIn) {
  const p = S.playing;
  if (!p) return false;
  p.stopped = true;
  try {
    p.source.stop();
  } catch {
    /* already ended */
  }
  if (bargeIn) send({ type: 'nova:bargeIn', id: p.id });
  return true;
}

// ------------------------------------------------------------------ control from the service worker
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.target !== 'offscreen') return false;
  (async () => {
    switch (msg.type) {
      case 'wake:start':
        await startWake();
        break;
      case 'wake:stop':
        stopWake();
        break;
      case 'wake:pause':
        if (S.mode !== 'off') setMode('paused');
        break;
      case 'wake:resume':
        if (S.mode !== 'command' && S.mode !== 'tentative') resumeListening();
        break;
      case 'wake:listen': // follow-up answer ("10 AM", "yes"): record without the wake word
      case 'voice:capture': { // push-to-talk from the orb or the dashboard
        const purpose = msg.type === 'voice:capture' ? 'dictate' : 'command';
        if (S.mode === 'command' && purpose === 'dictate') return { mode: S.mode, busy: true };
        const err = await openMic();
        if (err) return { mode: 'off', error: err };
        if (S.mode === 'off') setMode('idle');
        stopPlayback(false);
        chime();
        beginCommand(purpose, msg.answer ? ANSWER_ENDPOINT : COMMAND_ENDPOINT);
        break;
      }
      case 'voice:finish': // user tapped the mic again: they're done talking
        if (S.mode === 'command') finishCommand('manual');
        break;
      case 'wake:cancel':
        if (S.mode === 'command' || S.mode === 'tentative') resumeListening();
        break;
      case 'voice:play':
        play(msg.id, msg.audio);
        break;
      case 'voice:stopAudio':
        return { stopped: stopPlayback(false) };
      case 'voice:status':
        break;
      default:
        return { error: 'unknown' };
    }
    return { mode: S.mode, wakeWanted: S.wakeWanted, playing: !!S.playing };
  })().then(sendResponse, (e) => sendResponse({ error: String(e?.message || e) }));
  return true;
});

globalThis.novaListener = { S, start: startWake, stop: stopWake, onPartialWake, onFinal, play, stopPlayback }; // diagnostics / e2e
