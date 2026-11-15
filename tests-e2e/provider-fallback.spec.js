// TEST 5 over real HTTP: a live NOVA backend whose Gemini endpoint is down must fall back to Groq.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const API = 'http://127.0.0.1:8766';
const MOCK = 'http://127.0.0.1:8767';

test('TEST 5 — Gemini unavailable → Groq handles the request automatically', async () => {
  const hits = [];
  const mock = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      hits.push(req.url);
      if (req.url.startsWith('/gemini/')) return res.writeHead(503, { 'Content-Type': 'application/json' }).end('{"error":"unavailable"}');
      const content = JSON.stringify({ intent: 'create_task', title: 'Prepare demo', when: 'tomorrow', priority: 'high' });
      res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ choices: [{ message: { content } }] }));
    });
  });
  await new Promise((r) => mock.listen(8767, '127.0.0.1', r));
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'nova-fallback-'));
  const py = path.join(ROOT, 'backend', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const server = spawn(py, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8766', '--log-level', 'warning'], {
    cwd: path.join(ROOT, 'backend'),
    env: { ...process.env, DATABASE_URL: `sqlite:///${path.join(tmp, 'f.db').split(path.sep).join('/')}`, GEMINI_API_KEY: 'fake-g', GROQ_API_KEY: 'fake-q',
      GEMINI_BASE_URL: `${MOCK}/gemini`, GROQ_BASE_URL: `${MOCK}/groq`, LLM_MAX_RETRIES: '1', PREFERRED_PROVIDER: 'gemini' },
    stdio: 'inherit',
  });
  try {
    await expect.poll(() => fetch(`${API}/api/health`).then((r) => r.ok).catch(() => false), { timeout: 30000 }).toBe(true);
    const { token } = await (await fetch(`${API}/api/auth/register`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'K', email: 'f@example.com', timezone: 'Asia/Kolkata' }) })).json();
    const auth = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' };
    const r = await (await fetch(`${API}/api/assistant/message`, { method: 'POST', headers: auth, body: JSON.stringify({ session_id: 'x', message: 'I should get the demo ready by tomorrow, it is important' }) })).json();
    expect(r.provider).toBe('groq');
    expect(r.intent).toBe('create_task');
    expect(r.status).toBe('done');
    // Gemini: main model → backup model (both 503), then one retry on the backup only (the
    // overloaded main model is skipped for a minute) → Groq.
    expect(hits.filter((h) => h.startsWith('/gemini/'))).toEqual([
      '/gemini/models/gemini-3.8-flash:generateContent',
      '/gemini/models/gemini-3.5-flash:generateContent',
      '/gemini/models/gemini-3.5-flash:generateContent',
    ]);
    expect(hits.some((h) => h === '/groq/chat/completions')).toBe(true);
    const tasks = await (await fetch(`${API}/api/tasks`, { headers: auth })).json();
    expect(tasks[0]).toMatchObject({ title: 'Prepare demo', priority: 'high' });
    const health = await (await fetch(`${API}/api/health/providers`, { headers: auth })).json();
    expect(health.gemini.failures).toBe(2);
    expect(health.groq.successes).toBe(1);
  } finally {
    server.kill();
    mock.close();
    await new Promise((r) => setTimeout(r, 300)); // let uvicorn release the SQLite file
    try {
      fs.rmSync(tmp, { recursive: true, force: true });
    } catch {
      /* still locked; the OS temp cleaner gets it */
    }
  }
});
