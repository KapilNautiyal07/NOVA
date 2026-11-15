# NOVA: a voice assistant that lives in Chrome

NOVA is a Chrome extension I built that you can talk to while you browse. Say **"Hey NOVA,
remind me to watch this video tomorrow at 8 PM"** and it saves the video, sets a reminder in
your timezone, and when the notification fires, one click takes you back to it.

It's meant to *do* things rather than chat: reminders, alarms, tasks, saving websites,
downloading files, summarising the page you're on, and booking meetings on Google Calendar.

**Live demo:** _add your link here_ · **Demo video:** _add your link here_

---

## What it can do

| | Try saying |
|---|---|
| **Reminders & alarms** | "Set an alarm for my client meeting tomorrow at 11" · "Remind me about this after two hours" · "Move my 5 PM reminder to 6" |
| **Tasks** | "Create a task to prepare for my interview next Friday" · "Show my tasks for today" · "Mark interview prep as done" |
| **Website memory** | "Remember this website" · "Find that site where I was reading about LangGraph" · "Open the website I saved yesterday" |
| **Files** | "Save this PDF" · "Download this file to my project folder" |
| **The current page** | "What is this website about?" · "Summarise this page" (asks before reading the page) |
| **Google Calendar** | "Schedule my meeting for tomorrow" → "11 am, project discussion" → "haan" → done, with a link to the event |
| **Follow-ups** | "Do that tomorrow" · "Move it to 6" · "Open that website again" |

It understands everyday Indian English and Hinglish too: "haan", "theek hai", "nahi",
"11 baje", and typos like "tommorow".

## How it works

```
Chrome extension (MV3)                         FastAPI backend
┌───────────────────────────┐                 ┌───────────────────────────────────┐
│ "Hey NOVA" wake word      │   WAV clip      │ Speech-to-text (Gemini → Groq)    │
│ (Vosk, runs on-device)    │ ──────────────▶ │                                   │
│                           │                 │ LangGraph: intent → specialist    │
│ Orb on every page,        │   text + tab    │ agent (reminder, task, memory,    │
│ side-panel dashboard      │ ──────────────▶ │ file, page, calendar, chat)       │
│                           │                 │                                   │
│ chrome.alarms,            │   actions +     │ LLM router (Gemini ⇄ Groq,        │
│ notifications, downloads  │ ◀────────────── │ deadlines, fallbacks)             │
│                           │   spoken reply  │ Google Calendar (OAuth 2.0)       │
└───────────────────────────┘                 │ PostgreSQL (Neon) + Alembic       │
                                              └───────────────────────────────────┘
```

Some decisions I'm happy with:

- **The wake word never leaves your computer.** A small Vosk model (WASM) listens for
  "Hey NOVA" locally; audio is only sent to the server after it's sure it heard its name.
- **It doesn't hang when an AI provider does.** Every request has a time budget. If Gemini
  is overloaded it tries a backup Gemini model, then Groq, and if all else fails a
  rule-based parser still understands reminders and tasks. Understanding a command never
  takes longer than 8 s, even during a provider outage (it used to take up to 39 s).
- **Risky actions wait for a "yes".** Deleting things and creating calendar events are always
  confirmed first.
- **Google Calendar is done properly:** OAuth 2.0 with PKCE, only the `calendar.events`
  scope, tokens encrypted at rest (Fernet), and idempotent event creation so a retry can't
  double-book.
- **Voice conversations feel natural.** NOVA waits through short pauses, can be interrupted
  mid-sentence, and keeps listening for your answer after it asks a question.

**Stack:** Python, FastAPI, LangGraph, SQLAlchemy, Alembic, PostgreSQL · JavaScript (Chrome
Manifest V3, Shadow DOM, offscreen documents) · Gemini (text, speech-to-text, TTS) · Groq ·
Vosk · pytest · Playwright

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/API.md](docs/API.md).

---

## Running it locally

You'll need Python 3.11+, Chrome 116+ and Node 18+.

**1. Backend**

```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # macOS/Linux: cp .env.example .env
alembic upgrade head              # only for PostgreSQL; SQLite sets itself up
uvicorn app.main:app --port 8000
```

Check <http://localhost:8000/api/health>. The API docs are at `/docs`.

The `.env` values that matter most:

| Variable | What it's for |
|---|---|
| `DATABASE_URL` | PostgreSQL (`postgresql+psycopg://…`) or SQLite (`sqlite:///./nova.db`) |
| `GEMINI_API_KEY` | Main AI, transcription and voice ([get a key](https://aistudio.google.com/apikey)) |
| `GROQ_API_KEY` | Backup AI and fast backup transcription ([get a key](https://console.groq.com/keys)). Worth adding: Gemini's free tier only allows a few voice requests a minute. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REDIRECT_URI` | Google Calendar (optional, see below) |
| `TOKEN_ENCRYPTION_KEY` | Encrypts Google tokens; required in production |
| `REGISTRATION_SECRET` | If set, creating an account needs it |

Every option is explained in [backend/.env.example](backend/.env.example). Without any AI
keys NOVA still handles reminders, tasks, website memory and downloads using its rule
parser; only open-ended chat and summaries need a provider.

**2. Extension**

```bash
cd extension
npm install
npm run setup        # downloads the 41 MB wake-word model (one time)
```

Then open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and pick
the `extension/` folder. The setup page opens: enter your server URL
(`http://localhost:8000`), a name and an email, and click **Create account**.

Finally, go to **Settings → Voice** and click **Grant access** for the microphone (one time),
and turn on **"Hey NOVA"** if you want hands-free mode.

## Using it

- Click the **orb** in the bottom-right corner of any page, or press **Alt+Shift+N**.
- Tap 🎙 and speak, or just type. What NOVA heard shows up as your message with
  **Edit & resend** in case it misheard.
- With hands-free on, say **"Hey NOVA"** from any tab. After answering it listens for a
  follow-up, so you can say "make it 6 instead" without the wake word. Say "that's all" to
  end, or "Hey NOVA" to interrupt it.
- **Alt+Shift+D** opens the dashboard (reminders, tasks, saved sites, files, settings).

A typical exchange:

```
You:  Hey NOVA, schedule my meeting for tomorrow
NOVA: Sure. What time tomorrow, and what's it about?
You:  11 am, project discussion
NOVA: Just to confirm, should I schedule "Project discussion" tomorrow at 11:00 AM for 30 minutes?
You:  Haan
NOVA: Done! "Project discussion" is on your Google Calendar. [Open in Google Calendar]
```

## Google Calendar setup (optional)

1. In [Google Cloud Console](https://console.cloud.google.com/), enable the **Google Calendar API**.
2. Set up the **OAuth consent screen** as *External*, add the `calendar.events` scope, and
   add your own Gmail under **Test users**.
3. Create an **OAuth client ID** of type *Web application* with this redirect URI:
   `http://localhost:8000/api/integrations/google/callback` (it has to match exactly,
   including the port).
4. Put the client ID and secret in `backend/.env` and restart the server.

The first time you ask NOVA to schedule something, it explains why it needs access and asks
to connect. Click the button, or just say "yes", and approve on Google's page. If Google
isn't configured on the server, NOVA sets a meeting reminder instead.

---

## Deploying

The repo includes a [render.yaml](render.yaml) for [Render](https://render.com) (free tier).

1. Push the code to GitHub.
2. On Render: **New → Blueprint**, pick this repository, and fill in the secrets it asks
   for (`DATABASE_URL`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `TOKEN_ENCRYPTION_KEY`, …).
   Migrations run automatically on every deploy.
3. Once it's live, open `https://<your-service>.onrender.com/api/health`.
4. In the extension, open **Settings → Account**, click **Change** next to *Server*, enter
   that address and allow it when Chrome asks. You stay signed in if both servers share the
   same database; otherwise NOVA asks you to create an account on the new one.

Things to know:

- The free plan sleeps after 15 minutes without traffic, so the first request after that
  takes about a minute.
- With `REGISTRATION_SECRET` empty anyone can create an account and use your API quota. Set
  it if the server is just for you.
- For Google Calendar on a public server, add
  `https://<your-service>.onrender.com/api/integrations/google/callback` to the OAuth client
  and set it as `GOOGLE_REDIRECT_URI`. While the consent screen is in *Testing*, only your
  test users (up to 100) can connect, and their access expires after 7 days. Opening it to
  everyone needs Google's verification for the `calendar.events` scope.

---

## Tests

```powershell
cd backend;  pip install -r requirements-dev.txt;  pytest -q       # 259 tests
cd extension;  npm test                                            # 36 tests
cd tests-e2e;  npm install;  npx playwright install chromium;  npx playwright test   # 19 tests
```

The backend tests use SQLite and fake Gemini, Groq and Google servers, so they don't need
keys or a network. They cover time parsing (timezones, DST, recurrence), intent routing,
multi-turn conversations, provider fallback and deadlines, the OAuth flow (PKCE, forged or
replayed callbacks, token refresh and revocation) and every calendar scenario.

The end-to-end tests load the real extension into Chromium against a real backend: setting
reminders from a page, downloads, the Gemini → Groq fallback, a browser restart restoring
alarms, a spoken "Hey NOVA" command played through a fake microphone, and the full Google
Calendar flow from "Connect" to the created event. Set `HEADED=1` to watch them run.

## Privacy

- On every request NOVA sends the current tab's URL, title and description to *your*
  server. Page text is only sent when you ask for a summary and allow it.
- The microphone is used only after you tap 🎙 or say "Hey NOVA". Clips are transcribed with
  storage turned off and then thrown away.
- Calendar access is limited to `calendar.events`; tokens stay encrypted on the server and
  are revoked when you disconnect.
- **Settings → Privacy** has *Export my data* and *Delete all my data*.

## Troubleshooting

| Problem | Fix |
|---|---|
| The orb doesn't show | Reload the tab once after installing. It can't run on `chrome://` pages, the Web Store, or Chrome's PDF viewer. |
| "Can't reach the NOVA server" | Make sure the server is running (`/api/health`) and the URL in Settings matches. A server on Render's free plan takes about a minute to wake up after being idle. |
| Want to switch between local and deployed servers | Settings → Account → **Change** next to *Server*. |
| "Hey NOVA" doesn't react | Settings → Voice shows the listener status. If the model is missing, run `npm run setup` in `extension/` and reload NOVA. |
| The voice sometimes sounds robotic | Gemini's free voice quota ran out for a moment, so the browser voice was used. It switches back by itself. |
| Google says `redirect_uri_mismatch` | `GOOGLE_REDIRECT_URI` must match the OAuth client exactly, port included. |
| Google says "Access blocked" | Add your account under **Test users** on the consent screen. |
| Reminder didn't ring | Chrome has to be running, and notifications must be allowed in your OS settings. Missed ones show up when Chrome starts. |

## Known limitations

- Reminders only ring while Chrome is running.
- Downloads go to your Downloads folder; extensions can't touch other folders.
- Voice isn't streamed, so the final transcript arrives a second or two after you stop
  talking.
- The wake word is English-only and can miss in very noisy rooms.
- NOVA creates calendar events but doesn't edit or delete existing ones yet.

## Project layout

```
backend/
  app/agents/        LangGraph graph, intent classifier, one agent per feature
  app/llm/           Gemini ⇄ Groq router with deadlines and fallbacks
  app/voice/         Speech-to-text and text-to-speech
  app/integrations/  Google Calendar (OAuth + API)
  app/routers/       FastAPI routes
  app/timeparse.py   Natural-language dates and times
  alembic/           Database migrations
  tests/             pytest
extension/
  src/background/    Service worker: API calls, alarms, notifications, the voice loop
  src/offscreen/     Microphone, wake word and audio playback
  src/content/       The floating orb
  src/sidepanel/     Dashboard
tests-e2e/           Playwright tests
render.yaml          Deployment config for Render
```
