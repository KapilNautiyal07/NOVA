import assert from 'node:assert/strict';
import { test } from 'node:test';
import { downsample, encodeWav, Endpointer, isDismissal, isWake, NoiseFloor, rms, stripWake, verifyWake, WAKE_GRAMMAR } from '../src/lib/wake.js';

test('wake phrases are recognised, ordinary speech is not', () => {
  for (const t of ['hey nova', 'Hey NOVA', 'hi nova', '[unk] hey nova', 'hey nova [unk]']) assert.equal(isWake(t), true, t);
  for (const t of ['', 'nova', 'hey', '[unk]', 'hey novak', 'the supernova', 'okay nova', '[unk] nova [unk]', 'hey [unk] nova', 'hey there']) assert.equal(isWake(t), false, t);
});

test('grammar contains the wake phrases and an [unk] catch-all', () => {
  const g = JSON.parse(WAKE_GRAMMAR);
  assert.ok(g.includes('hey nova'));
  assert.ok(g.includes('[unk]'));
});

test('rms of silence is 0 and of a full-scale square wave is 1', () => {
  assert.equal(rms(new Float32Array(128)), 0);
  assert.equal(rms(Float32Array.from({ length: 128 }, (_, i) => (i % 2 ? 1 : -1))), 1);
});

const run = (ep, frames, step = 50) => {
  let t = 0;
  let out;
  for (const level of frames) {
    t += step;
    out = ep.push(level, t);
    if (!['listening', 'speaking'].includes(out)) return { out, t };
  }
  return { out, t };
};

test('a command ends after a pause that follows speech', () => {
  const ep = new Endpointer();
  const frames = [...Array(10).fill(0.003), ...Array(30).fill(0.08), ...Array(40).fill(0.003)];
  const { out, t } = run(ep, frames);
  assert.equal(out, 'done');
  assert.ok(t >= 2000 + 1100 && t < 2000 + 1400, `ended at ${t}`);
});

test('no speech at all times out', () => {
  const { out, t } = run(new Endpointer({ noSpeechMs: 3000 }), Array(200).fill(0.003));
  assert.equal(out, 'no_speech');
  assert.equal(t, 3000);
});

test('a short click is not treated as speech', () => {
  const frames = [...Array(10).fill(0.003), 0.2, 0.2, ...Array(200).fill(0.003)];
  assert.equal(run(new Endpointer({ noSpeechMs: 4000 }), frames).out, 'no_speech');
});

test('a noisy room raises the threshold, so steady noise is not mistaken for speech', () => {
  const nf = new NoiseFloor();
  for (let i = 0; i < 400; i++) nf.update(i % 7 === 0 ? 0.03 : 0.02); // fan noise while waiting for the wake word
  assert.ok(nf.value > 0.018 && nf.value < 0.03, `floor ${nf.value}`);
  const ep = new Endpointer({ floor: nf.value, noSpeechMs: 3000 });
  assert.ok(ep.threshold() > 0.05, `threshold ${ep.threshold()}`);
  assert.equal(run(ep, Array(100).fill(0.022)).out, 'no_speech');
  const ep2 = new Endpointer({ floor: nf.value });
  assert.equal(run(ep2, [...Array(20).fill(0.15), ...Array(40).fill(0.022)]).out, 'done');
});

test('noise floor drops quickly when the room gets quiet', () => {
  const nf = new NoiseFloor(0.05);
  for (let i = 0; i < 20; i++) nf.update(0.004);
  assert.ok(nf.value < 0.01, `floor ${nf.value}`);
});

test('long speech is capped', () => {
  assert.equal(run(new Endpointer({ maxMs: 5000 }), Array(200).fill(0.1)).out, 'max');
});

test('dismissals', () => {
  for (const t of ['never mind', 'Nevermind.', 'cancel', 'stop', "that's all"]) assert.equal(isDismissal(t), true, t);
  for (const t of ['cancel my 5 PM reminder', 'stop the download', 'remind me']) assert.equal(isDismissal(t), false, t);
});

test('downsample 48k → 16k keeps duration and averages', () => {
  const out = downsample(Float32Array.from({ length: 4800 }, () => 0.5), 48000);
  assert.equal(out.length, 1600);
  assert.ok(Math.abs(out[10] - 0.5) < 1e-6);
});

test('encodeWav writes a valid 16-bit mono header', () => {
  const wav = encodeWav(new Float32Array([0, 1, -1]), 16000);
  const v = new DataView(wav.buffer);
  assert.equal(String.fromCharCode(...wav.slice(0, 4)), 'RIFF');
  assert.equal(String.fromCharCode(...wav.slice(8, 12)), 'WAVE');
  assert.equal(v.getUint32(24, true), 16000);
  assert.equal(v.getUint32(40, true), 6);
  assert.equal(v.getInt16(46, true), 32767);
  assert.equal(v.getInt16(48, true), -32768);
});

test('stripWake removes the wake phrase from the transcript', () => {
  assert.equal(stripWake('Hey Nova, remind me in 5 minutes.'), 'remind me in 5 minutes.');
  assert.equal(stripWake('Nova. Save this file'), 'Save this file');
  assert.equal(stripWake('Remember this website'), 'Remember this website');
  assert.equal(stripWake('Novak Djokovic score'), 'Novak Djokovic score');
});

test('final-result verification accepts real wake phrases and rejects near misses', () => {
  const W = (s) => s.split(' ').map((p) => { const [word, conf] = p.split(':'); return { word, conf: Number(conf ?? 1) }; });
  assert.equal(verifyWake(W('hey:1 nova:1')), true);
  assert.equal(verifyWake(W('hey:1 nova:1 [unk]:1')), true);
  assert.equal(verifyWake(W('[unk]:1 hi:0.97 nova:0.92 [unk]:1')), true);
  // Recorded near misses (see tests-e2e voice evaluation):
  assert.equal(verifyWake(W('hey:1 nova:0.75 [unk]:1')), false, '"Hey Noah"');
  assert.equal(verifyWake(W('hi:1 know:1 what:1 [unk]:1')), false, '"Hi, know what…"');
  assert.equal(verifyWake(W('[unk]:1 a:1 nova:1 [unk]:1')), false, '"a nova is…"');
  assert.equal(verifyWake(W('hey:1 [unk]:1 nova:1')), false);
  assert.equal(verifyWake([]), false);
});

test('isThanks ends a conversation politely; isDismissal covers spoken goodbyes', async () => {
  const { isThanks, isDismissal: dismiss } = await import('../src/lib/wake.js');
  for (const t of ['thanks', 'Thank you.', 'ok thank you so much', 'great, thanks']) assert.equal(isThanks(t), true, t);
  for (const t of ['thanks for the reminder, now open youtube', 'remind me to thank Riya']) assert.equal(isThanks(t), false, t);
  for (const t of ["that's it", 'bye', 'no, that’s all'.replace('’', "'"), "okay that's all", 'nothing else']) assert.equal(dismiss(t), true, t);
});

test('Endpointer waits longer through a thinking pause when little has been said', () => {
  const ep = new Endpointer({ silenceMs: 1300, shortSpeechMs: 1300, hesitationMs: 2000, floor: 0.002 });
  ep.reset(0);
  let t = 0;
  const feed = (level, ms) => {
    let v;
    for (const end = t + ms; t < end; ) {
      t += 85;
      v = ep.push(level, t);
    }
    return v;
  };
  feed(0.2, 1000); // "Remind me to call"
  assert.equal(feed(0.001, 1600), 'speaking'); // 1.6 s pause: not done yet
  feed(0.2, 1500); // "the electricity office tomorrow morning"
  assert.equal(feed(0.001, 1200), 'speaking');
  assert.equal(feed(0.001, 200), 'done'); // after 2.5 s of speech the normal 1.3 s applies
});
