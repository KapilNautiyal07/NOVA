// NOVA floating companion (content script). Classic script — runs in an isolated world.
// UI lives in a shadow root so it can't be styled or read by the page, and page content is
// only collected on demand (URL/title/selection always; full text only with permission).
(() => {
  if (window.top !== window || window.__novaInjected) return;
  window.__novaInjected = true;

  const FILE_EXT = new Set(['pdf', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'csv', 'txt', 'md', 'epub', 'odt', 'rtf', 'zip', 'rar', '7z', 'gz', 'tar', 'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'mp3', 'mp4', 'wav', 'mov', 'json', 'xml', 'ipynb', 'exe', 'msi', 'dmg', 'apk', 'iso']);
  const TOOL_LABELS = {
    create_reminder: 'Scheduled reminder', save_website: 'Saved website', create_task: 'Created task', list_tasks: 'Loaded tasks',
    list_reminders: 'Loaded reminders', find_reminders: 'Matched reminders', cancel_reminder: 'Cancelled reminder', snooze_reminder: 'Snoozed',
    complete_task: 'Completed task', delete_task: 'Deleted task', find_tasks: 'Matched tasks', search_websites: 'Searched memory',
    create_file_record: 'Saved file details', search_files: 'Searched files', complete_reminder: 'Completed reminder',
    create_calendar_event: 'Added to Google Calendar', reschedule_reminder: 'Moved reminder', update_task: 'Updated task',
  };

  // ---------------------------------------------------------------- page context
  function fileLinks() {
    const out = [];
    const seen = new Set();
    const anchors = document.querySelectorAll('a[href]');
    for (let i = 0; i < anchors.length && i < 3000 && out.length < 25; i++) {
      const a = anchors[i];
      let u;
      try {
        u = new URL(a.href, location.href);
      } catch {
        continue;
      }
      if (!/^https?:$/.test(u.protocol) || seen.has(u.href)) continue;
      const ext = (u.pathname.split('/').pop() || '').includes('.') ? u.pathname.split('.').pop().toLowerCase().slice(0, 10) : null;
      if ((ext && FILE_EXT.has(ext)) || a.hasAttribute('download')) {
        seen.add(u.href);
        out.push({ url: u.href, text: (a.textContent || a.getAttribute('download') || '').trim().replace(/\s+/g, ' ').slice(0, 200), ext });
      }
    }
    return out;
  }

  function extractText() {
    const root = document.querySelector('article, main, [role="main"]') || document.body;
    if (!root) return '';
    const clone = root.cloneNode(true);
    // Never include anything the user may be typing (drafts, passwords) — only published page text.
    clone.querySelectorAll('script, style, noscript, svg, nav, footer, header, aside, form, iframe, input, textarea, select, [contenteditable]:not([contenteditable="false"]), [aria-hidden="true"], #nova-host').forEach((n) => n.remove());
    return (clone.textContent || '').replace(/[ \t ]+/g, ' ').replace(/\s*\n\s*/g, '\n').replace(/\n{3,}/g, '\n\n').trim().slice(0, 20000);
  }

  function collect(withText) {
    const selection = String(window.getSelection() || '').trim().slice(0, 4000);
    const meta = document.querySelector('meta[name="description"], meta[property="og:description"]');
    const ctx = {
      url: location.href,
      title: document.title,
      content_type: document.contentType,
      selection: selection || null,
      description: meta?.content?.slice(0, 500) || null,
      file_links: fileLinks(),
    };
    if (withText) ctx.text = extractText();
    return ctx;
  }

  // ---------------------------------------------------------------- messaging
  const send = (msg) =>
    new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(msg, (res) => {
          if (chrome.runtime.lastError) resolve({ ok: false, error: 'NOVA was updated — reload this page to reconnect.' });
          else resolve(res || { ok: false, error: 'No response' });
        });
      } catch {
        resolve({ ok: false, error: 'NOVA was updated — reload this page to reconnect.' });
      }
    });

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === 'nova:collect') {
      sendResponse(collect(!!msg.withText));
      return false;
    }
    if (msg?.type === 'nova:activate') {
      ui.ensure(true).then(() => {
        ui.open();
        if (msg.listen && settings.voice.enabled) voice.start();
        else ui.focus();
      });
      sendResponse({ ok: true });
      return false;
    }
    if (msg?.type === 'nova:ping') {
      sendResponse({ ok: true });
      return false;
    }
    if (msg?.type === 'nova:voice') {
      handsFree.on(msg).finally(() => sendResponse({ ok: true }));
      return true;
    }
    if (msg?.type === 'nova:dictation') {
      voice.onDictation(msg);
      sendResponse({ ok: true });
      return false;
    }
    if (msg?.type === 'nova:speech') {
      voice.onSpeech(msg);
      sendResponse({ ok: true });
      return false;
    }
    return false;
  });

  // ---------------------------------------------------------------- settings
  let settings = null;
  let debugOpenShadow = false;
  const DEFAULTS = {
    orbEnabled: true, animations: true, suggestions: true, pageAccess: 'ask', theme: 'auto',
    voice: { enabled: true, wakeWord: false, lang: '', tts: true, engine: 'gemini', geminiVoice: 'Kore', conversation: true, voiceName: '', rate: 1 },
    disabledSites: [], pageAccessSites: [], orbPosition: null,
  };
  async function loadSettings() {
    const s = await chrome.storage.local.get(['settings', 'debugOpenShadow']);
    debugOpenShadow = !!s.debugOpenShadow;
    settings = { ...DEFAULTS, ...(s.settings || {}), voice: { ...DEFAULTS.voice, ...(s.settings?.voice || {}) } };
    return settings;
  }
  const host = location.hostname;
  const siteEnabled = () => settings.orbEnabled && !settings.disabledSites.includes(host);

  // ---------------------------------------------------------------- UI
  const CSS = `
  :host { all: initial; }
  [hidden] { display: none !important; }
  .root { --bg: rgba(18,18,26,.92); --surface: rgba(255,255,255,.06); --border: rgba(255,255,255,.10); --text: #EDEDF4; --muted: #9A9AAD;
    --accent: #7C6CFF; --accent2: #22D3EE; --ok: #34D399; --err: #F87171; --user: linear-gradient(135deg,#7C6CFF,#5B8CFF);
    position: fixed; z-index: 2147483646; font: 13px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--text); }
  .root.light { --bg: rgba(255,255,255,.96); --surface: rgba(17,17,30,.05); --border: rgba(17,17,30,.10); --text: #15151E; --muted: #6B6B7B; }
  * { box-sizing: border-box; }
  button { font: inherit; color: inherit; cursor: pointer; }
  .orb { position: relative; width: 52px; height: 52px; border-radius: 50%; border: 0; padding: 0; background: transparent; touch-action: none; display: block; outline: none; }
  .orb:focus-visible { box-shadow: 0 0 0 3px rgba(124,108,255,.5); }
  .core { position: absolute; inset: 6px; border-radius: 50%;
    background: radial-gradient(circle at 32% 28%, #fff 0, #C9C2FF 12%, #7C6CFF 42%, #3B2FBF 72%, #1A1446 100%);
    box-shadow: 0 6px 22px rgba(124,108,255,.45), inset 0 -4px 10px rgba(0,0,0,.35); animation: breathe 4s ease-in-out infinite; transition: filter .3s, transform .2s; }
  .orb:hover .core { transform: scale(1.06); }
  .ring { position: absolute; inset: 0; border-radius: 50%; opacity: 0; }
  .wave { position: absolute; inset: 15px 13px; display: flex; gap: 3px; align-items: center; justify-content: center; opacity: 0; }
  .wave i { width: 3px; height: 30%; border-radius: 2px; background: #fff; animation: wave 1s ease-in-out infinite; }
  .wave i:nth-child(2){animation-delay:.12s}.wave i:nth-child(3){animation-delay:.24s}.wave i:nth-child(4){animation-delay:.36s}.wave i:nth-child(5){animation-delay:.48s}
  .glyph { position: absolute; inset: 0; display: grid; place-items: center; font-size: 20px; color: #fff; opacity: 0; transform: scale(.6); transition: .25s; font-weight: 700; }
  [data-state="listening"] .core { background: radial-gradient(circle at 32% 28%, #fff 0, #B9F3FF 14%, #22D3EE 45%, #0E7490 78%, #082F49 100%); box-shadow: 0 6px 26px rgba(34,211,238,.55); }
  [data-state="listening"] .ring { opacity: 1; border: 2px solid rgba(34,211,238,.7); animation: ripple 1.4s ease-out infinite; }
  [data-state="thinking"] .ring, [data-state="executing"] .ring { opacity: 1; inset: -3px; background: conic-gradient(from 0deg, transparent 0 60%, var(--accent2) 85%, var(--accent) 100%);
    -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 3px)); mask: radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 3px)); animation: spin .9s linear infinite; }
  [data-state="executing"] .ring { background: conic-gradient(from 0deg, transparent 0 40%, var(--ok) 100%); animation-duration: .6s; }
  [data-state="thinking"] .core { animation: pulse 1.1s ease-in-out infinite; }
  [data-state="speaking"] .wave { opacity: 1; }
  [data-state="success"] .core { background: radial-gradient(circle at 32% 28%, #fff 0, #BBF7D0 14%, #34D399 45%, #047857 80%, #022C22 100%); box-shadow: 0 6px 24px rgba(52,211,153,.5); }
  [data-state="success"] .glyph.ok, [data-state="error"] .glyph.err { opacity: 1; transform: scale(1); }
  [data-state="error"] .core { background: radial-gradient(circle at 32% 28%, #fff 0, #FECACA 14%, #F87171 45%, #B91C1C 80%, #450A0A 100%); }
  [data-state="error"] .orb { animation: shake .4s ease-in-out; }
  .noanim *, .noanim *::before, .noanim *::after { animation: none !important; transition: none !important; }
  @media (prefers-reduced-motion: reduce) { * { animation-duration: 0s !important; animation-iteration-count: 1 !important; } }
  @keyframes breathe { 50% { transform: scale(1.045); filter: brightness(1.08); } }
  @keyframes pulse { 50% { transform: scale(.92); filter: brightness(1.25); } }
  @keyframes ripple { from { transform: scale(.9); opacity: .9; } to { transform: scale(1.55); opacity: 0; } }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes wave { 0%,100% { height: 25%; } 50% { height: 95%; } }
  @keyframes shake { 25% { transform: translateX(-4px); } 75% { transform: translateX(4px); } }
  @keyframes rise { from { opacity: 0; transform: translateY(8px) scale(.98); } to { opacity: 1; transform: none; } }
  .panel, .bubble { position: absolute; background: var(--bg); border: 1px solid var(--border); border-radius: 16px; box-shadow: 0 24px 60px rgba(0,0,0,.35), 0 2px 8px rgba(0,0,0,.2);
    backdrop-filter: blur(18px) saturate(1.4); -webkit-backdrop-filter: blur(18px) saturate(1.4); animation: rise .18s ease-out; }
  .panel { width: 360px; height: min(520px, calc(100vh - 100px)); display: flex; flex-direction: column; overflow: hidden; }
  .panel[hidden], .bubble[hidden] { display: none; }
  header { display: flex; align-items: center; gap: 10px; padding: 12px 12px 10px 14px; border-bottom: 1px solid var(--border); }
  .dot { width: 18px; height: 18px; border-radius: 50%; background: radial-gradient(circle at 32% 28%, #fff 0, #7C6CFF 45%, #1A1446 100%); flex: none; }
  .ttl { font-weight: 650; letter-spacing: .2px; }
  .status { color: var(--muted); font-size: 12px; margin-left: 2px; }
  .sp { flex: 1; }
  .icon { width: 28px; height: 28px; border-radius: 8px; border: 0; background: transparent; color: var(--muted); display: grid; place-items: center; }
  .icon:hover { background: var(--surface); color: var(--text); }
  .icon svg { width: 16px; height: 16px; }
  .msgs { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 10px; scroll-behavior: smooth; }
  .msg { max-width: 88%; padding: 9px 12px; border-radius: 14px; white-space: pre-wrap; word-wrap: break-word; animation: rise .16s ease-out; }
  .msg.user { align-self: flex-end; background: var(--user); color: #fff; border-bottom-right-radius: 4px; }
  .msg.bot { align-self: flex-start; background: var(--surface); border: 1px solid var(--border); border-bottom-left-radius: 4px; }
  .msg.bot.error { border-color: rgba(248,113,113,.45); }
  .msg.interim { opacity: .65; font-style: italic; }
  .steps { align-self: flex-start; display: flex; flex-wrap: wrap; gap: 6px; margin-top: -4px; }
  .step { font-size: 11px; color: var(--muted); padding: 2px 8px; border-radius: 99px; border: 1px solid var(--border); }
  .step.ok::before { content: "✓ "; color: var(--ok); } .step.bad::before { content: "✕ "; color: var(--err); }
  .links { align-self: stretch; display: flex; flex-direction: column; gap: 6px; }
  .link { text-align: left; padding: 8px 10px; border-radius: 10px; border: 1px solid var(--border); background: transparent; display: flex; flex-direction: column; }
  .link:hover { background: var(--surface); } .link small { color: var(--muted); }
  .card { align-self: stretch; border: 1px solid var(--border); border-radius: 12px; padding: 12px; background: var(--surface); }
  .card p { margin: 0 0 10px; } .row { display: flex; gap: 8px; flex-wrap: wrap; }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; padding: 0 14px 8px; }
  .chip, .btn { border: 1px solid var(--border); background: var(--surface); padding: 6px 10px; border-radius: 99px; font-size: 12px; }
  .chip:hover, .btn:hover { border-color: var(--accent); }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  form { display: flex; align-items: center; gap: 6px; padding: 10px; border-top: 1px solid var(--border); }
  input { flex: 1; min-width: 0; height: 38px; border-radius: 10px; border: 1px solid var(--border); background: var(--surface); color: var(--text); padding: 0 12px; font: inherit; outline: none; }
  input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(124,108,255,.18); }
  .mic, .send { width: 38px; height: 38px; border-radius: 10px; border: 1px solid var(--border); background: var(--surface); display: grid; place-items: center; flex: none; }
  .mic svg, .send svg { width: 18px; height: 18px; }
  .mic.on { background: #0E7490; border-color: #22D3EE; color: #fff; animation: pulse 1.2s infinite; }
  .send { background: var(--accent); border-color: var(--accent); color: #fff; }
  .bubble { width: 280px; padding: 12px 12px 10px; }
  .bubble p { margin: 0 24px 10px 0; } .bubble .x { position: absolute; top: 6px; right: 6px; }
  .empty { color: var(--muted); text-align: center; margin: auto; padding: 0 20px; } .empty b { color: var(--text); display: block; font-size: 15px; margin-bottom: 6px; }
  /* Hands-free ("Hey NOVA") */
  .halo { position: absolute; inset: -10px; border-radius: 50%; pointer-events: none; opacity: 0;
    background: radial-gradient(circle, rgba(34,211,238,.55) 0, rgba(34,211,238,.18) 45%, transparent 70%);
    transform: scale(calc(.85 + var(--lvl, 0) * .7)); transition: transform .12s ease-out, opacity .2s; }
  [data-state="listening"] .halo { opacity: calc(.35 + var(--lvl, 0) * .65); }
  [data-state="listening"] .core { animation: none; transform: scale(calc(1 + var(--lvl, 0) * .12)); transition: transform .12s ease-out; }
  .root.summoned .orb { animation: summon .45s cubic-bezier(.2,1.4,.4,1); }
  @keyframes summon { 0% { transform: scale(.4); opacity: 0; } 100% { transform: none; opacity: 1; } }
  .hud { position: absolute; width: 300px; padding: 10px 12px; cursor: pointer; display: flex; flex-direction: column; gap: 4px; }
  .hud .tag { font-size: 11px; font-weight: 650; letter-spacing: .4px; text-transform: uppercase; color: var(--accent2); display: flex; align-items: center; gap: 6px; }
  .hud .tag::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
  [data-state="listening"] .hud .tag::before { animation: pulse 1s ease-in-out infinite; }
  .hud .you { color: var(--muted); font-style: italic; }
  .hud .you:empty, .hud .say:empty { display: none; }
  .hud .say { max-height: 9.5em; overflow: hidden; }
  .hud.err .tag { color: var(--err); }
  /* live audio waveform (listening: your voice; speaking: NOVA's voice) */
  .bars { display: none; align-items: center; gap: 3px; height: 22px; }
  [data-state="listening"] .bars, [data-state="speaking"] .bars { display: flex; }
  .bars i { width: 4px; border-radius: 3px; background: var(--accent2); height: calc(4px + var(--lvl, 0) * 18px * var(--k, 1)); transition: height .09s ease-out; }
  [data-state="speaking"] .bars i { background: var(--accent); }
  .bars i:nth-child(1){--k:.45}.bars i:nth-child(2){--k:.8}.bars i:nth-child(3){--k:1}.bars i:nth-child(4){--k:.7}.bars i:nth-child(5){--k:.5}.bars i:nth-child(6){--k:.85}.bars i:nth-child(7){--k:.4}
  [data-state="speaking"] .wave i { animation: none; height: calc(20% + var(--lvl, 0) * 80% * var(--k, 1)); transition: height .09s; }
  .wave i:nth-child(1){--k:.6}.wave i:nth-child(2){--k:.9}.wave i:nth-child(3){--k:1}.wave i:nth-child(4){--k:.8}.wave i:nth-child(5){--k:.55}
  .live { align-self: stretch; display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-radius: 12px; border: 1px dashed rgba(34,211,238,.5); color: var(--muted); }
  .live span { flex: 1; font-style: italic; }
  .msg.user .edit { display: block; margin: 6px 0 0 auto; font-size: 11px; padding: 1px 8px; border-radius: 99px; border: 1px solid rgba(255,255,255,.45); background: transparent; color: #fff; opacity: .85; }
  .msg.user .edit:hover { opacity: 1; background: rgba(255,255,255,.15); }
  .msg.user .via { font-size: 10px; opacity: .75; display: block; margin-top: 2px; }
  .hud .acts { display: flex; gap: 6px; margin-top: 4px; }
  .hud .acts:empty { display: none; }
  `;
  const ICONS = {
    mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>',
    send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
    expand: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  };
  const STATUS_TEXT = { idle: 'Ready', listening: 'Listening…', thinking: 'Thinking…', speaking: 'Speaking', executing: 'Working…', success: 'Done', error: 'Something went wrong' };

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  const ui = {
    hostEl: null, root: null, state: 'idle', stateTimer: null, built: false, lastAsk: null,
    async ensure(force = false) {
      if (!settings) await loadSettings();
      if (this.built) {
        this.hostEl.style.display = '';
        return;
      }
      if (!force && !siteEnabled()) return;
      this.build();
    },
    build() {
      this.built = true;
      this.hostEl = document.createElement('div');
      this.hostEl.id = 'nova-host';
      const shadow = this.hostEl.attachShadow({ mode: debugOpenShadow ? 'open' : 'closed' });
      const style = el('style');
      style.textContent = CSS;
      this.root = el('div', 'root');
      this.root.dataset.state = 'idle';
      this.root.innerHTML = `
        <div class="bubble" hidden><button class="icon x" data-act="dismiss" aria-label="Dismiss">${ICONS.close}</button><p></p><div class="row"></div></div>
        <div class="bubble hud" hidden role="status" aria-live="polite" title="Open the conversation"><span class="tag"></span><span class="bars" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></span><span class="you"></span><span class="say"></span><span class="acts"></span></div>
        <section class="panel" hidden role="dialog" aria-label="NOVA assistant">
          <header><div class="dot"></div><div><span class="ttl">NOVA</span> <span class="status">Ready</span></div><div class="sp"></div>
            <button class="icon" data-act="stop" title="Stop speaking" hidden>${ICONS.stop}</button>
            <button class="icon" data-act="dashboard" title="Open dashboard">${ICONS.expand}</button>
            <button class="icon" data-act="close" title="Close (Esc)">${ICONS.close}</button></header>
          <div class="msgs" role="log" aria-live="polite"><div class="empty"><b>Hi, I'm NOVA.</b>Try “Remind me to visit this page tomorrow at 10 AM”, “Save this PDF”, or “Show my tasks”.</div></div>
          <div class="chips"></div>
          <form><button type="button" class="mic" aria-label="Speak to NOVA">${ICONS.mic}</button>
            <input type="text" maxlength="2000" placeholder="Ask NOVA or type a command…" aria-label="Message NOVA" autocomplete="off">
            <button type="submit" class="send" aria-label="Send">${ICONS.send}</button></form>
        </section>
        <button class="orb" aria-label="Open NOVA assistant" title="NOVA (Alt+Shift+N)"><span class="halo"></span><span class="ring"></span><span class="core"></span>
          <span class="wave"><i></i><i></i><i></i><i></i><i></i></span><span class="glyph ok">✓</span><span class="glyph err">!</span></button>`;
      shadow.append(style, this.root);
      document.documentElement.appendChild(this.hostEl);
      this.$ = (s) => this.root.querySelector(s);
      this.panel = this.$('.panel');
      this.msgs = this.$('.msgs');
      this.input = this.$('input');
      this.applyTheme();
      this.place();
      this.bind();
    },
    applyTheme() {
      const dark = settings.theme === 'dark' || (settings.theme === 'auto' && matchMedia('(prefers-color-scheme: dark)').matches);
      this.root.classList.toggle('light', !dark);
      this.root.classList.toggle('noanim', !settings.animations);
    },
    place() {
      const pos = settings.orbPosition || { right: 24, bottom: 24 };
      const right = Math.min(Math.max(pos.right, 8), innerWidth - 60);
      const bottom = Math.min(Math.max(pos.bottom, 8), innerHeight - 60);
      Object.assign(this.root.style, { right: `${right}px`, bottom: `${bottom}px` });
      const openUp = bottom < innerHeight / 2;
      const alignRight = right < innerWidth / 2;
      for (const box of [this.panel, this.$('.bubble'), this.$('.hud')]) {
        Object.assign(box.style, { bottom: openUp ? '64px' : '', top: openUp ? '' : '64px', right: alignRight ? '0' : '', left: alignRight ? '' : '0' });
      }
    },
    bind() {
      const orb = this.$('.orb');
      let start = null;
      let dragged = false;
      orb.addEventListener('pointerdown', (e) => {
        start = { x: e.clientX, y: e.clientY, right: parseFloat(this.root.style.right), bottom: parseFloat(this.root.style.bottom) };
        dragged = false;
        orb.setPointerCapture(e.pointerId);
      });
      orb.addEventListener('pointermove', (e) => {
        if (!start) return;
        const dx = e.clientX - start.x;
        const dy = e.clientY - start.y;
        if (!dragged && Math.hypot(dx, dy) < 5) return;
        dragged = true;
        this.root.style.right = `${Math.min(Math.max(start.right - dx, 8), innerWidth - 60)}px`;
        this.root.style.bottom = `${Math.min(Math.max(start.bottom - dy, 8), innerHeight - 60)}px`;
      });
      orb.addEventListener('pointerup', async () => {
        if (!start) return;
        start = null;
        if (dragged) {
          settings.orbPosition = { right: parseFloat(this.root.style.right), bottom: parseFloat(this.root.style.bottom) };
          const s = (await chrome.storage.local.get('settings')).settings || {};
          await chrome.storage.local.set({ settings: { ...s, orbPosition: settings.orbPosition } });
          this.place();
          return;
        }
        if (this.state === 'speaking') return voice.stopSpeaking();
        if (this.state === 'listening' && handsFree.active) return void send({ type: 'nova:voiceCancel' });
        if (this.state === 'listening') return voice.stop();
        this.toggle();
      });
      orb.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          this.toggle();
        }
      });
      this.root.addEventListener('click', (e) => {
        const act = e.target.closest('[data-act]')?.dataset.act;
        if (act === 'close') this.close();
        else if (act === 'dashboard') send({ type: 'nova:openDashboard' });
        else if (act === 'dismiss') this.$('.bubble').hidden = true;
        else if (act === 'stop') voice.stopSpeaking();
        else if (e.target.closest('.hud .acts')) return; // buttons inside the caption handle themselves
        else if (e.target.closest('.hud')) {
          this.$('.hud').hidden = true;
          this.open();
        }
      });
      this.$('form').addEventListener('submit', (e) => {
        e.preventDefault();
        const text = this.input.value.trim();
        if (!text) return;
        this.input.value = '';
        assistant.ask(text, { viaVoice: false });
      });
      this.$('.mic').addEventListener('click', () => (voice.active ? voice.stop() : voice.start()));
      this.root.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          e.stopPropagation();
          if (this.state === 'speaking') voice.stopSpeaking();
          else this.close();
        }
      });
      matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => this.applyTheme());
      addEventListener('resize', () => this.place());
    },
    open() {
      this.panel.hidden = false;
      this.$('.bubble').hidden = true;
      this.$('.hud').hidden = true;
      this.place();
      this.focus();
    },
    close() {
      this.panel.hidden = true;
      voice.stop();
      voice.stopSpeaking();
    },
    toggle() {
      this.panel.hidden ? this.open() : this.close();
    },
    focus() {
      setTimeout(() => this.input?.focus(), 30);
    },
    setState(state, holdMs) {
      clearTimeout(this.stateTimer);
      this.state = state;
      if (!this.root) return;
      this.root.dataset.state = state;
      this.$('.status').textContent = STATUS_TEXT[state] || '';
      this.$('[data-act="stop"]').hidden = state !== 'speaking';
      this.$('.mic').classList.toggle('on', state === 'listening');
      if (holdMs) this.stateTimer = setTimeout(() => this.setState('idle'), holdMs);
    },
    clearEmpty() {
      this.msgs.querySelector('.empty')?.remove();
    },
    add(role, text, { error = false } = {}) {
      this.clearEmpty();
      const m = el('div', `msg ${role}${error ? ' error' : ''}`, text);
      this.msgs.appendChild(m);
      this.scroll();
      return m;
    },
    addNode(node) {
      this.clearEmpty();
      this.msgs.appendChild(node);
      this.scroll();
    },
    scroll() {
      this.msgs.scrollTop = this.msgs.scrollHeight;
    },
    chips(list) {
      const box = this.$('.chips');
      box.replaceChildren();
      for (const c of list) {
        const b = el('button', 'chip', c.label);
        b.type = 'button';
        b.addEventListener('click', () => {
          box.replaceChildren();
          c.run();
        });
        box.appendChild(b);
      }
    },
    suggestion(res) {
      const bubble = this.$('.bubble');
      bubble.querySelector('p').textContent = res.message;
      const row = bubble.querySelector('.row');
      row.replaceChildren();
      for (const a of res.actions || []) {
        const b = el('button', 'chip', a.label);
        b.addEventListener('click', () => {
          this.open();
          assistant.ask(a.command, { viaVoice: false });
        });
        row.appendChild(b);
      }
      bubble.hidden = !this.panel.hidden; // never cover an open conversation
      setTimeout(() => (bubble.hidden = true), 12000);
    },
  };

  // ---------------------------------------------------------------- assistant flow
  const assistant = {
    busy: false,
    // User's message bubble. Voice input shows what NOVA understood and can be corrected.
    addUser(text, { viaVoice = false } = {}) {
      const m = ui.add('user', text);
      if (viaVoice) {
        m.appendChild(el('span', 'via', '🎙 heard'));
        const b = el('button', 'edit', 'Edit & resend');
        b.type = 'button';
        b.title = 'Fix what NOVA heard and send it again';
        b.addEventListener('click', () => {
          if (ui.panel.hidden) ui.open();
          ui.input.value = text;
          ui.input.focus();
          ui.input.select();
        });
        m.appendChild(b);
      }
      return m;
    },
    async ask(text, { viaVoice = false, withText = false, quiet = false } = {}) {
      if (this.busy) return;
      this.busy = true;
      ui.chips([]);
      if (!withText && !quiet) this.addUser(text, { viaVoice });
      ui.setState('thinking');
      const page = collect(withText);
      const res = await send({ type: 'nova:ask', message: text, page, pageTextAllowed: withText, channel: viaVoice ? 'voice' : 'text' });
      this.busy = false;
      if (!res.ok) {
        const msg = res.offline ? 'I can’t reach the NOVA server. Check your internet connection and that the server is running, then try again.' : res.error || 'Something went wrong.';
        ui.add('bot', msg, { error: true });
        ui.setState('error', 2500);
        const chips = [{ label: 'Try again', run: () => this.ask(text, { viaVoice, withText, quiet: true }) }];
        if (res.status === 401) chips.push({ label: 'Open settings', run: () => send({ type: 'nova:openOptions' }) });
        ui.chips(chips);
        if (viaVoice) voice.speak(msg);
        return;
      }
      this.render(res.data, text, viaVoice);
    },
    render(r, text, viaVoice) {
      ui.add('bot', r.reply, { error: r.status === 'error' });
      const tools = (r.trace || []).filter((t) => t.node.startsWith('tool:') && !/^tool:(list|find|search)_/.test(t.node));
      if (tools.length) {
        const steps = el('div', 'steps');
        for (const t of tools) steps.appendChild(el('span', `step ${t.ok ? 'ok' : 'bad'}`, TOOL_LABELS[t.node.slice(5)] || t.node.slice(5).replace(/_/g, ' ')));
        ui.addNode(steps);
      }
      for (const a of r.actionResults || []) {
        if (a.type === 'download' && a.ok) {
          const s = el('div', 'steps');
          s.appendChild(el('span', 'step ok', 'Download started'));
          ui.addNode(s);
        }
        if (!a.ok && !a.deferred) ui.add('bot', `Couldn't ${a.type.replace('_', ' ')}: ${a.error}`, { error: true });
      }
      const sites = r.data?.websites || [];
      if (sites.length && r.intent === 'search_websites') {
        const box = el('div', 'links');
        for (const s of sites.slice(0, 5)) {
          const b = el('button', 'link');
          b.append(el('span', '', s.title), el('small', '', s.domain));
          b.addEventListener('click', () => send({ type: 'nova:openUrl', url: s.url }));
          box.appendChild(b);
        }
        ui.addNode(box);
      }
      for (const a of r.client_actions || []) {
        if (a.type === 'show_link' && a.payload?.url) {
          const box = el('div', 'links');
          const b = el('button', 'link');
          b.append(el('span', '', a.payload.label || 'Open link'), el('small', '', new URL(a.payload.url).hostname));
          b.addEventListener('click', () => send({ type: 'nova:openUrl', url: a.payload.url }));
          box.appendChild(b);
          ui.addNode(box);
        }
        if (a.type === 'connect_calendar') this.calendarCard(viaVoice || handsFree.active, !!a.payload?.auto);
      }
      const chips = [];
      const permission = (r.client_actions || []).find((a) => a.type === 'request_page_content');
      if (permission) this.permission(text, viaVoice);
      for (const s of r.suggestions || []) chips.push({ label: s, run: () => this.ask(s, { viaVoice }) });
      const view = (r.client_actions || []).find((a) => a.type === 'show_view');
      if (view) chips.push({ label: 'Open in dashboard', run: () => send({ type: 'nova:openDashboard', view: view.payload.view }) });
      ui.chips(chips);
      const ok = r.status === 'done';
      const executed = tools.length > 0 || (r.actionResults || []).some((a) => !a.deferred);
      if (r.status === 'error') ui.setState('error', 2500);
      else if (ok && executed) {
        ui.setState('executing');
        setTimeout(() => ui.setState('success', 1600), 450);
      } else ui.setState('idle');
      // A spoken question gets a spoken answer: listen again as soon as NOVA finishes asking.
      if (viaVoice) setTimeout(() => voice.speak(r.reply, { listen: expectsAnswer(r) }), ok && executed ? 500 : 0);
    },
    // "Connect Google Calendar": OAuth starts only when the user clicks this button.
    calendarCard(viaVoice, auto = false) {
      const card = el('div', 'card');
      if (auto) {
        // The user said "yes" to connecting — open Google's page now instead of asking for a click.
        card.appendChild(el('p', '', 'Opening Google’s sign-in page…'));
        ui.addNode(card);
        return this.connectCalendar(card, viaVoice);
      }
      card.appendChild(el('p', '', 'NOVA will open Google’s sign-in page. You choose whether to allow access — NOVA only asks to add events to your calendar, and it always confirms the details with you before creating anything.'));
      const row = el('div', 'row');
      const go = el('button', 'btn primary', 'Connect Google Calendar');
      const no = el('button', 'btn', 'Not now');
      go.type = no.type = 'button';
      go.addEventListener('click', () => this.connectCalendar(card, viaVoice));
      no.addEventListener('click', () => {
        card.remove();
        this.ask('No, don’t connect my calendar', { viaVoice, quiet: true });
      });
      row.append(go, no);
      card.appendChild(row);
      ui.addNode(card);
      if (handsFree.active || ui.panel.hidden) handsFree.hudAction('Connect Google Calendar', () => this.connectCalendar(card, viaVoice));
    },
    async connectCalendar(card, viaVoice) {
      const p = card.querySelector('p');
      card.querySelectorAll('button').forEach((b) => (b.disabled = true));
      const res = await send({ type: 'nova:calendarConnect' });
      if (!res.ok) {
        p.textContent = res.status === 503 ? 'Google Calendar isn’t set up on your NOVA server yet (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).' : res.error || 'Couldn’t start Google sign-in.';
        card.querySelectorAll('button').forEach((b) => (b.disabled = false));
        return;
      }
      p.textContent = 'Waiting for you to approve access in the Google tab…';
      const started = Date.now();
      const poll = async () => {
        const st = await send({ type: 'nova:calendarStatus', attempt: res.data.attempt_id });
        const outcome = st.ok ? st.data.attempt?.outcome : null;
        if (outcome === 'connected') {
          card.remove();
          if (ui.panel.hidden) ui.open();
          return this.ask('I’ve connected my Google Calendar.', { viaVoice });
        }
        if (outcome === 'denied') {
          p.textContent = st.data.attempt.detail === 'scope_not_granted'
            ? 'Calendar access wasn’t ticked on Google’s page, so nothing was connected.'
            : 'You didn’t allow access, so Google Calendar isn’t connected. Nothing was changed.';
          card.querySelectorAll('button').forEach((b) => (b.disabled = false));
          if (viaVoice) voice.speak('Okay, I didn’t connect your calendar.');
          return;
        }
        if (outcome === 'error' || outcome === 'expired' || Date.now() - started > 10 * 60000) {
          p.textContent = 'Connecting didn’t finish. You can try again.';
          card.querySelectorAll('button').forEach((b) => (b.disabled = false));
          return;
        }
        setTimeout(poll, 2000);
      };
      setTimeout(poll, 2000);
    },
    async permission(text, viaVoice) {
      if (settings.pageAccess === 'never') {
        ui.add('bot', 'Page reading is turned off in NOVA settings, so I can only use the page title and address.');
        return;
      }
      if (settings.pageAccess === 'always' || settings.pageAccessSites.includes(host)) return this.ask(text, { viaVoice, withText: true });
      const card = el('div', 'card');
      card.appendChild(el('p', '', `Allow NOVA to read this page's text? Only the visible text (up to 6,000 characters) is sent to the AI provider for this request. It isn't stored.`));
      const row = el('div', 'row');
      const mk = (label, cls, fn) => {
        const b = el('button', `btn ${cls}`, label);
        b.addEventListener('click', () => {
          card.remove();
          fn();
        });
        row.appendChild(b);
      };
      mk('Allow once', 'primary', () => this.ask(text, { viaVoice, withText: true }));
      mk(`Always on ${host}`, '', async () => {
        const s = (await chrome.storage.local.get('settings')).settings || {};
        const sites = Array.from(new Set([...(s.pageAccessSites || []), host]));
        await chrome.storage.local.set({ settings: { ...s, pageAccessSites: sites } });
        settings.pageAccessSites = sites;
        this.ask(text, { viaVoice, withText: true });
      });
      mk('Not now', '', () => ui.add('bot', "No problem — I won't read this page."));
      card.appendChild(row);
      ui.addNode(card);
    },
  };

  // Same rule as lib/wake.js expectsAnswer (content scripts can't import modules): NOVA asked
  // something — but not while the user is busy on Google's own sign-in page.
  function expectsAnswer(r) {
    const auto = (r.client_actions || []).some((a) => a.type === 'connect_calendar' && a.payload?.auto);
    return ['needs_confirmation', 'needs_clarification'].includes(r.status) || (r.status === 'needs_authorization' && !auto);
  }

  // ---------------------------------------------------------------- voice (push-to-talk)
  // Recording, recognition and speech all run in NOVA's background voice engine (one mic
  // permission for every site, Gemini transcription, natural Gemini voice). This only shows it.
  const voice = {
    active: false, live: null, speaking: false,
    async start() {
      if (!settings.voice.enabled) {
        ui.add('bot', 'Voice input is turned off in settings — type your request instead.');
        return ui.focus();
      }
      if (ui.panel.hidden) ui.open();
      this.stopSpeaking();
      this.active = true;
      ui.setState('listening');
      this.showLive('Listening… speak naturally, I’ll wait for you to finish.');
      const res = await send({ type: 'nova:dictate' });
      if (!res.ok || !res.data?.ok) {
        const d = res.data || {};
        this.active = false;
        this.clearLive();
        if (d.error === 'mic_denied') {
          ui.add('bot', 'NOVA needs one-time microphone permission. It works on every site after that, and you can keep typing meanwhile.', { error: true });
          ui.chips([{ label: '🎙 Allow microphone', run: () => send({ type: 'nova:openMicGrant' }) }]);
          return ui.setState('error', 2500);
        }
        return this.fail(d.message || res.error || 'Couldn’t start voice input. Type your request instead.');
      }
    },
    stop() {
      if (this.active) send({ type: 'nova:dictateStop' }); // finish now; what was said is kept
    },
    cancel() {
      if (!this.active) return;
      this.active = false;
      this.clearLive();
      send({ type: 'nova:dictateCancel' });
      if (ui.state === 'listening') ui.setState('idle');
    },
    onDictation(m) {
      if (m.phase === 'level') return void ui.root?.style.setProperty('--lvl', Math.max(0, Math.min(1, Number(m.level) || 0)).toFixed(3));
      if (m.phase === 'partial') return this.showLive(m.text, true);
      if (m.phase === 'transcribing') {
        ui.setState('thinking');
        return this.showLive(this.live?.querySelector('span')?.dataset.partial ? this.live.querySelector('span').textContent : 'Understanding…');
      }
      this.active = false;
      this.clearLive();
      ui.root?.style.setProperty('--lvl', '0');
      if (m.phase === 'final') return assistant.ask(m.text, { viaVoice: true });
      if (m.phase === 'nospeech') {
        ui.add('bot', 'I didn’t hear anything. Tap the mic and try again — or type.');
        return ui.setState('idle');
      }
      if (m.phase === 'error') this.fail(m.message || 'Voice input failed. You can type instead.');
    },
    showLive(text, partial = false) {
      if (!this.live) {
        this.live = el('div', 'live');
        const bars = el('span', 'bars');
        bars.innerHTML = '<i></i><i></i><i></i><i></i><i></i><i></i><i></i>';
        bars.style.flex = 'none';
        this.live.append(bars, el('span', ''));
        ui.addNode(this.live);
      }
      const span = this.live.querySelector('span:not(.bars)');
      span.textContent = text;
      if (partial) span.dataset.partial = '1';
      ui.scroll();
    },
    clearLive() {
      this.live?.remove();
      this.live = null;
    },
    fail(message) {
      this.active = false;
      ui.add('bot', message, { error: true });
      ui.setState('error', 2500);
      ui.focus();
    },
    speak(text, { listen = false } = {}) {
      if (!settings.voice.tts || !text) return;
      this.speaking = true;
      this.listenAfter = listen && !handsFree.active; // hands-free listens for follow-ups itself
      send({ type: 'nova:speak', text });
    },
    onSpeech(m) {
      if (m.state === 'start') {
        this.speaking = true;
        if (!handsFree.active) ui.setState('speaking');
      } else if (m.state === 'level') {
        ui.root?.style.setProperty('--lvl', Math.max(0, Math.min(1, Number(m.level) || 0)).toFixed(3));
      } else if (m.state === 'end') {
        this.speaking = false;
        ui.root?.style.setProperty('--lvl', '0');
        if (ui.state === 'speaking' && !handsFree.active) ui.setState('idle');
        const listen = this.listenAfter && m.outcome !== 'interrupted' && !handsFree.active && !this.active;
        this.listenAfter = false;
        if (listen) this.start();
      }
    },
    stopSpeaking() {
      this.listenAfter = false; // the user cut NOVA off: don't open the mic on them
      if (this.speaking || handsFree.active || ui.state === 'speaking') send({ type: 'nova:ttsStop' });
      this.speaking = false;
      if (ui.state === 'speaking') ui.setState('idle');
    },
  };

  // ---------------------------------------------------------------- hands-free ("Hey NOVA")
  // The service worker runs the exchange; this only mirrors it on the orb and a caption bubble.
  const handsFree = {
    active: false, hideTimer: null,
    hud(tag, { you, say, error = false } = {}) {
      const box = ui.$('.hud');
      clearTimeout(this.hideTimer);
      box.classList.toggle('err', error);
      box.querySelector('.tag').textContent = tag;
      if (you !== undefined) box.querySelector('.you').textContent = you ? `“${you}”` : '';
      if (say !== undefined) box.querySelector('.say').textContent = say || '';
      box.hidden = !ui.panel.hidden; // the open panel shows the conversation itself
      ui.$('.bubble:not(.hud)').hidden = true;
    },
    // A button inside the caption (e.g. "Connect Google Calendar") for when the panel is closed.
    hudAction(label, run) {
      const acts = ui.$('.hud .acts');
      acts.replaceChildren();
      const b = el('button', 'btn primary', label);
      b.type = 'button';
      b.addEventListener('click', (e) => {
        e.stopPropagation();
        acts.replaceChildren();
        ui.$('.hud').hidden = true;
        ui.open();
        run();
      });
      acts.appendChild(b);
      clearTimeout(this.hideTimer);
    },
    hideHudSoon(ms = 6000) {
      if (ui.$('.hud .acts').childElementCount) return; // keep a pending action visible
      clearTimeout(this.hideTimer);
      this.hideTimer = setTimeout(() => (ui.$('.hud').hidden = true), ms);
    },
    async on(msg) {
      await ui.ensure(true); // you called NOVA, so show the orb even where it's normally hidden
      const { phase } = msg;
      if (phase === 'level') return void ui.root.style.setProperty('--lvl', Math.max(0, Math.min(1, Number(msg.level) || 0)).toFixed(3));
      if (phase === 'listening') {
        this.active = true;
        voice.cancel();
        ui.$('.hud .acts').replaceChildren();
        ui.root.classList.remove('summoned');
        void ui.root.offsetWidth; // restart the pop-in animation
        ui.root.classList.add('summoned');
        ui.root.style.setProperty('--lvl', '0');
        ui.setState('listening');
        this.hud(msg.followUp ? 'Listening — anything else?' : 'Listening…', { you: '', say: msg.followUp ? undefined : '' });
      } else if (phase === 'thinking') {
        ui.setState('thinking');
        this.hud('Thinking…');
      } else if (phase === 'partial') {
        this.hud(ui.state === 'listening' ? 'Listening…' : 'Thinking…', { you: msg.text });
      } else if (phase === 'heard') {
        assistant.addUser(msg.text, { viaVoice: true }); // the panel log keeps what NOVA understood (editable)
        this.hud('Working on it…', { you: msg.text, say: '' });
      } else if (phase === 'result') {
        const r = msg.data || {};
        assistant.render(r, msg.text, false);
        if (r.status === 'needs_permission' && ui.panel.hidden) ui.open(); // the Allow card lives in the panel
        this.hud(r.status === 'error' ? 'Problem' : r.status === 'done' ? 'Done' : 'NOVA', { say: r.reply, error: r.status === 'error' });
      } else if (phase === 'speaking') {
        ui.setState('speaking');
      } else if (phase === 'error') {
        ui.setState('error', 2500);
        this.hud('NOVA', { say: msg.message, error: true });
        this.hideHudSoon(5000);
      } else if (phase === 'done') {
        this.active = false;
        if (['listening', 'thinking', 'speaking'].includes(ui.state)) ui.setState('idle');
        ui.root.style.setProperty('--lvl', '0');
        this.hideHudSoon();
      }
    },
  };

  // ---------------------------------------------------------------- lifecycle
  let lastUrl = location.href;
  async function onPage() {
    send({ type: 'nova:visit', url: location.href, title: document.title });
    if (!ui.built || !settings.suggestions) return;
    const res = await send({ type: 'nova:suggest', page: collect(false) });
    if (res.ok && res.data?.message && ui.panel.hidden) ui.suggestion(res.data);
  }

  chrome.storage.onChanged.addListener(async (changes, area) => {
    if (area !== 'local' || !changes.settings) return;
    await loadSettings();
    if (siteEnabled()) {
      await ui.ensure();
      ui.applyTheme();
    } else if (ui.built && ui.panel.hidden) ui.hostEl.style.display = 'none';
  });

  loadSettings().then(async () => {
    await ui.ensure();
    setTimeout(onPage, 2500);
    setInterval(() => {
      if (location.href !== lastUrl) {
        lastUrl = location.href;
        setTimeout(onPage, 1500); // SPA navigation
      }
    }, 2000);
  });

  window.__nova = { collect, ui, assistant, voice }; // isolated world only — not visible to the page
})();
