// Pure logic for the hands-free voice loop (unit-tested with node --test):
// which phrases wake NOVA, and when the user has finished speaking a command.

// Vosk grammar: the recognizer may only output these phrases. "[unk]" absorbs other speech, and
// the decoys give similar-sounding words ("let us go", "the nova part", "hey there") somewhere to
// land, so they aren't forced onto the wake phrase. Only "hey/hi" directly followed by "nova" wakes.
export const WAKE_PHRASES = ['hey nova', 'hi nova'];
const DECOYS = ['hey', 'hi', 'okay', 'nova', 'noah', 'nora', 'no one', 'no', 'know', 'now', 'go', 'over', 'over there', 'there', 'they', 'the', 'a', 'hello', 'novel', 'nobody', 'know what'];
export const WAKE_GRAMMAR = JSON.stringify([...WAKE_PHRASES, ...DECOYS, '[unk]']);

export function isWake(text) {
  const t = String(text || '').toLowerCase().replace(/[^a-z ]/g, ' ').replace(/\s+/g, ' ').trim();
  return WAKE_PHRASES.some((p) => t === p || t.endsWith(` ${p}`) || t.startsWith(`${p} `) || t.includes(` ${p} `));
}

// Root-mean-square level of one audio frame (Float32 samples in -1..1).
export function rms(samples) {
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return samples.length ? Math.sqrt(sum / samples.length) : 0;
}

// Long-running background-noise estimate, updated on every frame while NOVA waits for the wake
// word. Falls quickly when the room gets quieter and rises slowly (~15 s) when it gets louder,
// so speech barely moves it but a fan or AC that switches on is learned.
export class NoiseFloor {
  constructor(initial = null) {
    this.value = initial;
  }

  update(level) {
    if (this.value === null) this.value = level;
    else this.value = level < this.value ? this.value * 0.85 + level * 0.15 : this.value * 0.995 + level * 0.005;
    return this.value;
  }
}

// Decides when a spoken command starts and ends from per-frame loudness.
// Seeded with the room's noise floor; it keeps adapting while nobody is talking.
export class Endpointer {
  constructor({
    minThreshold = 0.012, // absolute floor for "speech" (normalised RMS)
    ratio = 2.8, // speech must be this many times louder than the background
    silenceMs = 1100, // a pause this long after speech ends the command
    noSpeechMs = 6000, // give up if nothing is said for this long
    maxMs = 15000, // hard cap on one command
    minSpeechMs = 250, // ignore clicks/bumps shorter than this
    floor = null, // background level measured before the command (see NoiseFloor)
    // People pause to think mid-sentence ("Remind me to call… the electricity office"). While
    // only a little has been said, wait longer before deciding the user has finished.
    shortSpeechMs = 0, // below this much speech…
    hesitationMs = 0, // …a pause must last this long to end the command
  } = {}) {
    Object.assign(this, { minThreshold, ratio, silenceMs, noSpeechMs, maxMs, minSpeechMs, shortSpeechMs, hesitationMs, initialFloor: floor });
    this.reset(0);
  }

  requiredSilence() {
    return this.totalSpeechMs < this.shortSpeechMs ? Math.max(this.silenceMs, this.hesitationMs) : this.silenceMs;
  }

  reset(now) {
    this.startedAt = now;
    this.floor = this.initialFloor;
    this.speechMs = 0;
    this.totalSpeechMs = 0;
    this.heardSpeech = false;
    this.lastLoudAt = null;
    this.lastAt = now;
  }

  threshold() {
    return Math.max(this.minThreshold, (this.floor ?? this.minThreshold / this.ratio) * this.ratio);
  }

  // Feed one frame; returns 'listening' | 'speaking' | 'done' | 'no_speech' | 'max'.
  push(level, now) {
    const dt = Math.max(0, now - this.lastAt);
    this.lastAt = now;
    const loud = level >= this.threshold();
    if (loud) {
      this.speechMs += dt;
      if (this.heardSpeech || this.speechMs >= this.minSpeechMs) this.totalSpeechMs += dt;
      this.lastLoudAt = now;
      if (this.speechMs >= this.minSpeechMs) this.heardSpeech = true;
    } else {
      // Track the background level only while quiet (slow attack, faster release).
      this.floor = this.floor === null ? level : level < this.floor ? this.floor * 0.7 + level * 0.3 : this.floor * 0.97 + level * 0.03;
      if (!this.heardSpeech && this.lastLoudAt !== null && now - this.lastLoudAt > 400) this.speechMs = 0; // a blip, not speech
    }
    if (now - this.startedAt >= this.maxMs) return this.heardSpeech ? 'max' : 'no_speech';
    if (this.heardSpeech && !loud && now - this.lastLoudAt >= this.requiredSilence()) return 'done';
    if (!this.heardSpeech && now - this.startedAt >= this.noSpeechMs) return 'no_speech';
    return this.heardSpeech ? 'speaking' : 'listening';
  }
}

// Statuses after which NOVA expects an answer, so it keeps listening without the wake word.
export const FOLLOW_UP_STATUSES = new Set(['needs_confirmation', 'needs_clarification']);

// NOVA said "yes" to connecting and is opening Google's sign-in page itself.
export function autoConnects(res) {
  return (res?.client_actions || []).some((a) => a.type === 'connect_calendar' && a.payload?.auto);
}

// Did NOVA just ask the user something they'll answer out loud? ("What time tomorrow?",
// "Should I schedule…?", "Would you like to connect your Google Calendar?"). Not while the
// user is busy on Google's own sign-in page.
export function expectsAnswer(res) {
  if (!res) return false;
  if (FOLLOW_UP_STATUSES.has(res.status)) return true;
  return res.status === 'needs_authorization' && !autoConnects(res);
}

// Phrases that end a voice exchange without sending anything to the assistant.
export function isDismissal(text) {
  return /^(?:ok(?:ay)?,?\s*)?(never ?mind|cancel( that)?|stop( listening)?|nothing|forget it|that'?s (all|it)|no thanks?|no,? that'?s all|thanks?,? that'?s all|bye|goodbye|i'?m done|that'?ll be all|nothing else)[.!]*$/i.test(String(text || '').trim());
}

// A spoken reply that just ends the conversation politely ("thank you") — answered, then NOVA stops listening.
export function isThanks(text) {
  return /^(?:ok(?:ay)?,?\s*)?(thanks?|thank you|thank you so much|thanks a lot|great,? thanks|perfect,? thanks|cool,? thanks)( nova)?[.!]*$/i.test(String(text || '').trim());
}

// Averages blocks of samples down to `to` Hz (Whisper works at 16 kHz; smaller uploads).
export function downsample(samples, from, to = 16000) {
  if (from <= to) return samples;
  const ratio = from / to;
  const out = new Float32Array(Math.floor(samples.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(samples.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j++) sum += samples[j];
    out[i] = sum / Math.max(1, end - start);
  }
  return out;
}

// 16-bit mono PCM WAV file from Float32 samples.
export function encodeWav(samples, sampleRate) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const str = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  str(0, 'RIFF');
  v.setUint32(4, 36 + samples.length * 2, true);
  str(8, 'WAVE');
  str(12, 'fmt ');
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); // PCM
  v.setUint16(22, 1, true); // mono
  v.setUint32(24, sampleRate, true);
  v.setUint32(28, sampleRate * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  str(36, 'data');
  v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Uint8Array(buf);
}

// Removes the wake phrase (captured in the pre-roll) from the start of a transcript.
export function stripWake(text) {
  return String(text || '')
    .replace(/^\s*((hey|hi|okay|ok|a)[\s,]+)?(nova|noah|nover|knova)\b[\s,.!?:-]*/i, '')
    .trim();
}

// Second-stage check on Vosk's *final* result (words with confidences). Partial results are fast
// but unstable ("Hey Noah", "hi, know what…" can briefly read as "hey nova"); the final result
// either drops "nova" or gives it a low confidence for those. Real wake phrases score ~1.0.
export const WAKE_MIN_CONF = 0.85;
export function verifyWake(words, minConf = WAKE_MIN_CONF) {
  const w = (words || []).map((x) => ({ word: String(x.word || '').toLowerCase(), conf: Number(x.conf) || 0 }));
  for (let i = 0; i + 1 < w.length; i++) {
    if ((w[i].word === 'hey' || w[i].word === 'hi') && w[i + 1].word === 'nova' && w[i].conf >= minConf && w[i + 1].conf >= minConf) return true;
  }
  return false;
}
