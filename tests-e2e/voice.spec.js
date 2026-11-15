// Hands-free "Hey NOVA": real extension + real on-device wake-word model (Vosk/WASM in the
// offscreen document) fed a synthesized recording through Chromium's fake microphone.
// Gemini (transcription + TTS via the Interactions API) and Groq are mocked, so no API keys are needed.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium, expect, test } from '@playwright/test';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PORT = 8768;
const MOCK_PORT = 8769;
const SITE_PORT = 8770;
const API = `http://127.0.0.1:${PORT}`;
const TZ = 'Asia/Kolkata';
const MODEL = path.join(ROOT, 'extension', 'models', 'vosk-model-small-en-us-0.15.tar.gz');

test.skip(!fs.existsSync(MODEL), 'Wake-word model missing: run "npm install && npm run setup" in extension/');
test.setTimeout(120000);

function listen(server, port) {
  return new Promise((r) => server.listen(port, '127.0.0.1', r));
}

// 0.6 s, 24 kHz mono 16-bit sine: a real WAV for the offscreen player to decode and play.
function toneWav() {
  const rate = 24000;
  const n = Math.round(rate * 0.6);
  const buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(36 + n * 2, 4); buf.write('WAVEfmt ', 8); buf.writeUInt32LE(16, 16);
  buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22); buf.writeUInt32LE(rate, 24); buf.writeUInt32LE(rate * 2, 28);
  buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34); buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) buf.writeInt16LE(Math.round(Math.sin((i / rate) * 2 * Math.PI * 440) * 8000), 44 + i * 2);
  return buf.toString('base64');
}

async function startStack(transcript) {
  const hits = [];
  const mock = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const body = Buffer.concat(chunks);
      const hit = { url: req.url, bytes: body.length, isWav: body.includes(Buffer.from('WAVEfmt ')) };
      hits.push(hit);
      const json = (code, obj) => res.writeHead(code, { 'Content-Type': 'application/json' }).end(JSON.stringify(obj));
      if (req.url === '/gemini/interactions') {
        const b = JSON.parse(body.toString());
        hit.model = b.model;
        if (b.model.includes('transcribe')) {
          const audio = Buffer.from(b.input[0].data, 'base64');
          Object.assign(hit, { kind: 'stt', isWav: audio.includes(Buffer.from('WAVEfmt ')), audioBytes: audio.length, config: b.generation_config.transcription_config });
          return json(200, { status: 'completed', steps: [{ type: 'model_output', content: [{ type: 'text', text: transcript }] }] });
        }
        Object.assign(hit, { kind: 'tts', text: b.input[0].content[0].text, voice: b.generation_config.speech_config[0].voice });
        return json(200, { status: 'completed', steps: [{ type: 'model_output', content: [{ type: 'audio', mime_type: 'audio/wav', data: toneWav() }] }] });
      }
      if (req.url === '/groq/audio/transcriptions') return json(200, { text: transcript });
      json(503, { error: 'down' }); // chat → rule parser
    });
  });
  await listen(mock, MOCK_PORT);

  const site = http.createServer((_req, res) => res.writeHead(200, { 'Content-Type': 'text/html' }).end('<!doctype html><title>Kitchen timer notes</title><h1>Recipes</h1><p>Bake for 25 minutes.</p>'));
  await listen(site, SITE_PORT);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'nova-voice-'));
  const py = path.join(ROOT, 'backend', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const server = spawn(py, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(PORT), '--log-level', 'warning'], {
    cwd: path.join(ROOT, 'backend'),
    env: {
      ...process.env,
      DATABASE_URL: `sqlite:///${path.join(tmp, 'v.db').split(path.sep).join('/')}`,
      GEMINI_API_KEY: 'fake-g',
      GEMINI_BASE_URL: `http://127.0.0.1:${MOCK_PORT}/gemini`,
      GROQ_API_KEY: 'fake-q',
      GROQ_BASE_URL: `http://127.0.0.1:${MOCK_PORT}/groq`,
      PREFERRED_PROVIDER: 'groq',
      LLM_MAX_RETRIES: '0',
      RATE_LIMIT_PER_MINUTE: '1000',
    },
    stdio: ['ignore', 'inherit', 'inherit'],
  });
  await expect.poll(() => fetch(`${API}/api/health`).then((r) => r.ok).catch(() => false), { timeout: 30000 }).toBe(true);

  const ext = path.join(tmp, 'extension');
  fs.cpSync(path.join(ROOT, 'extension'), ext, { recursive: true, filter: (s) => !s.includes(`${path.sep}test`) && !s.includes('node_modules') });
  const mf = JSON.parse(fs.readFileSync(path.join(ext, 'manifest.json'), 'utf8'));
  mf.host_permissions.push(`${API}/*`);
  fs.writeFileSync(path.join(ext, 'manifest.json'), JSON.stringify(mf, null, 2));

  const stop = async () => {
    server.kill();
    mock.close();
    site.close();
    await new Promise((r) => setTimeout(r, 500)); // let Chromium/uvicorn release their files
    try {
      fs.rmSync(tmp, { recursive: true, force: true }); // extension copy + profile with the unpacked model (~150 MB)
    } catch {
      /* a straggler process still holds a file; the OS temp cleaner gets it */
    }
  };
  return { hits, tmp, ext, stop };
}

async function launch(stack, wav) {
  return chromium.launchPersistentContext(fs.mkdtempSync(path.join(stack.tmp, 'profile-')), {
    channel: 'chromium',
    headless: !process.env.HEADED,
    timezoneId: TZ,
    args: [
      `--disable-extensions-except=${stack.ext}`,
      `--load-extension=${stack.ext}`,
      '--use-fake-ui-for-media-stream', // auto-grant the microphone
      '--use-fake-device-for-media-stream',
      `--use-file-for-fake-audio-capture=${wav}%noloop`,
      '--autoplay-policy=no-user-gesture-required',
    ],
  });
}

async function connect(context) {
  const sw = context.serviceWorkers()[0] || (await context.waitForEvent('serviceworker'));
  const reg = await fetch(`${API}/api/auth/register`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'Kapil', email: `voice-${Date.now()}@example.com`, timezone: TZ }) });
  const { token, user } = await reg.json();
  for (const p of context.pages()) if (p.url().includes('/options/')) await p.close().catch(() => {});
  await sw.evaluate(({ token, user, api }) => chrome.storage.local.set({ token, user, backendUrl: api, debugOpenShadow: true, settings: { suggestions: false } }), { token, user, api: API });
  const call = async (p) => (await fetch(API + p, { headers: { Authorization: `Bearer ${token}` } })).json();
  return { sw, call };
}

test('“Hey NOVA, remind me to check the oven in five minutes” works hands-free', async () => {
  const stack = await startStack('Hey Nova, remind me to check the oven in five minutes.');
  const context = await launch(stack, path.join(ROOT, 'tests-e2e', 'fixtures', 'hey-nova-remind.wav'));
  try {
    const { sw, call } = await connect(context);
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${SITE_PORT}/`);
    await expect(page.locator('#nova-host .orb')).toBeVisible({ timeout: 10000 });
    await page.bringToFront();

    // Turn on "Hey NOVA": the service worker opens the offscreen listener, which loads the model
    // and then opens the (fake) microphone, which starts playing the recording.
    const before = Date.now();
    await sw.evaluate(() => chrome.storage.local.get('settings').then(({ settings }) => chrome.storage.local.set({ settings: { ...settings, voice: { wakeWord: true } } })));

    // The orb pops up in the listening state when the wake word is heard on-device.
    const host = page.locator('#nova-host');
    await expect(host.locator('.root')).toHaveAttribute('data-state', 'listening', { timeout: 45000 });
    await expect(host.locator('.hud .tag')).toContainText('Listening');

    // The recorded command goes to Gemini transcription (smart mode, Indian/US English hints) as WAV.
    await expect.poll(() => sw.evaluate(() => globalThis.nova.voiceLoop.state.finalSeq), { timeout: 30000 }).toBeGreaterThan(0);
    await expect.poll(() => stack.hits.some((h) => h.kind === 'stt'), { timeout: 30000 }).toBe(true);
    const upload = stack.hits.filter((h) => h.kind === 'stt').reduce((a, h) => (h.audioBytes > a.audioBytes ? h : a));
    expect(upload.isWav).toBe(true);
    expect(upload.audioBytes).toBeGreaterThan(32000); // > 1 s of 16 kHz audio
    expect(upload.config).toMatchObject({ mode: 'smart', language_codes: ['en-IN', 'en-US'] });
    // Live captions while speaking go to Groq (fast, saves Gemini quota); the final transcript
    // used for the command is Gemini's (checked above via the full-length clip).

    await expect(host.locator('.hud .you')).toContainText('remind me to check the oven', { timeout: 15000 });
    await expect.poll(async () => (await call('/api/reminders')).length, { timeout: 15000 }).toBe(1);
    const [rem] = await call('/api/reminders');
    expect(rem.title.toLowerCase()).toContain('check the oven');
    const due = new Date(rem.scheduled_at).getTime();
    expect(due).toBeGreaterThan(before + 4 * 60000);
    expect(due).toBeLessThan(Date.now() + 6 * 60000);

    // A real chrome.alarms entry rings it; the orb shows the reply, then returns to idle.
    await expect.poll(() => sw.evaluate(async () => (await chrome.alarms.getAll()).filter((a) => a.name.startsWith('nova:')).length), { timeout: 10000 }).toBeGreaterThan(0);
    await expect(host.locator('.hud .say')).toContainText(/remind/i);

    // The reply is spoken with Gemini TTS (markdown/emoji stripped) and played by the voice engine.
    await expect.poll(() => stack.hits.some((h) => h.kind === 'tts'), { timeout: 15000 }).toBe(true);
    const tts = stack.hits.find((h) => h.kind === 'tts');
    expect(tts.text).toMatch(/remind you/i);
    expect(tts.voice).toBe('Kore');
    // Conversation mode: NOVA listens for a follow-up, hears nothing, and ends quietly.
    await expect(host.locator('.hud .tag')).toContainText('anything else', { timeout: 15000 });
    await expect(host.locator('.root')).not.toHaveAttribute('data-state', /listening|thinking|speaking/, { timeout: 30000 });

    // And the listener goes back to waiting for "Hey NOVA".
    await expect.poll(() => sw.evaluate(async () => (await chrome.storage.session.get('wakeStatus')).wakeStatus?.mode), { timeout: 30000 }).toBe('wake');
  } finally {
    await context.close();
    await stack.stop();
  }
});

test('speech without the wake word does not trigger NOVA', async () => {
  const stack = await startStack('should never be requested');
  const context = await launch(stack, path.join(ROOT, 'tests-e2e', 'fixtures', 'no-wake.wav'));
  try {
    const { sw } = await connect(context);
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${SITE_PORT}/`);
    await sw.evaluate(() => chrome.storage.local.get('settings').then(({ settings }) => chrome.storage.local.set({ settings: { ...settings, voice: { wakeWord: true } } })));
    await expect.poll(() => sw.evaluate(async () => (await chrome.storage.session.get('wakeStatus')).wakeStatus?.mode), { timeout: 45000 }).toBe('wake');
    await page.waitForTimeout(12000); // the whole recording plays
    expect(stack.hits.filter((h) => h.kind === 'stt' || h.url === '/groq/audio/transcriptions')).toHaveLength(0);
    await expect(page.locator('#nova-host .root')).not.toHaveAttribute('data-state', 'listening');
  } finally {
    await context.close();
    await stack.stop();
  }
});

test('push-to-talk on the orb: Gemini transcript is shown, editable, and acted on', async () => {
  const stack = await startStack('Remind me to check the oven in five minutes.');
  const context = await launch(stack, path.join(ROOT, 'tests-e2e', 'fixtures', 'hey-nova-remind.wav'));
  try {
    const { call } = await connect(context);
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${SITE_PORT}/`);
    const host = page.locator('#nova-host');
    await expect(host.locator('.orb')).toBeVisible({ timeout: 10000 });
    await host.locator('.orb').click();
    await host.locator('.mic').click(); // recording happens in the extension's voice engine, not the page

    await expect(host.locator('.live')).toBeVisible();
    await expect(host.locator('.msg.user')).toContainText('Remind me to check the oven', { timeout: 45000 });
    await expect(host.locator('.msg.user .via')).toHaveText('🎙 heard');
    await expect.poll(async () => (await call('/api/reminders')).length, { timeout: 20000 }).toBe(1);
    await expect(host.locator('.msg.bot').last()).toContainText(/remind you/i);
    expect(stack.hits.filter((h) => h.kind === 'stt').length).toBeGreaterThan(0);
    // the reply is spoken with the natural voice
    await expect.poll(() => stack.hits.some((h) => h.kind === 'tts'), { timeout: 15000 }).toBe(true);

    // Correcting what NOVA heard: the text goes back into the input box for editing.
    await host.locator('.msg.user .edit').click();
    await expect(host.locator('input')).toHaveValue('Remind me to check the oven in five minutes.');
  } finally {
    await context.close();
    await stack.stop();
  }
});
