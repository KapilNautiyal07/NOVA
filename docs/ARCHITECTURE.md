# NOVA architecture

NOVA has two halves. The **Chrome extension** (Manifest V3) does everything that has to
happen inside the browser: reading page context, voice input and output, scheduling,
notifications, downloads, and opening tabs. The **Python backend** holds the system of
record and the LangGraph agent that turns a sentence into validated actions.

```
┌──────────────────────────────── Chrome ────────────────────────────────┐
│ Content script  src/content/orb.js   (every http/https page)           │
│   • Floating orb + mini chat in a Shadow DOM (page CSS can't leak in)  │
│   • Page context: URL, title, domain, meta description, file links     │
│   • Page TEXT only after permission (per-site or "always"), ≤20k chars │
│   • Voice: Web Speech API → fallback MediaRecorder clip → server STT   │
│                                                                        │
│ Side panel / dashboard  src/sidepanel/   Popup  src/popup/             │
│ Options (onboarding + settings)  src/options/                          │
│                                                                        │
│ Service worker  src/background/service-worker.js                       │
│   • The only component that talks to the backend (holds the token)     │
│   • Executes client actions returned by the agent:                     │
│       sync_reminders · open_url · download · show_view ·               │
│       request_page_content                                             │
│   • chrome.alarms + chrome.notifications (Open / Snooze / Done)        │
│   • Local mirror of pending reminders → rebuilds alarms on startup     │
│   • Offline outbox for fired/snoozed/completed acknowledgements        │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │  HTTP + "Authorization: Bearer <device token>"
┌──────────────────────────────────▼─────────────────────────────────────┐
│ FastAPI  app/main.py                                                   │
│   /api/assistant/message ─► agents/runner.py ─► LangGraph (agents/)    │
│   /api/reminders /tasks /websites /files  (routers/resources.py)       │
│   /api/auth/*  /api/data/*  /api/voice/transcribe                      │
│                                                                        │
│ services/  — the only code that writes to the DB (used by REST + tools) │
│ llm/providers.py — Gemini ⇄ Groq router with retries and cooldowns     │
│ SQLAlchemy 2 ─► PostgreSQL (SQLite for dev/tests) · Alembic migrations │
│ Redis (optional) ─► rate limiting                                      │
└────────────────────────────────────────────────────────────────────────┘
```

## The agent graph

`agents/graph.py` compiles one LangGraph `StateGraph`. Each user turn runs it once:

```
START ─► browser_context ─┬─ pending step? ─► resume ─┬─► <specialist> ─► respond ─► END
                          │                           ├─► classify
                          │                           └─► respond   (user said "no")
                          └──────────────────────────► classify ─► <specialist> ─► respond ─► END
```

| Node | Role |
|---|---|
| `browser_context` (**Browser Context Agent**) | Builds a sanitised page profile from the raw tab info. It classifies the page type (video, GitHub repo, docs, PDF, job listing, article…), strips zero-width and bidi characters from any page text, and caps its length. |
| `resume` | Continues a pending step from the previous turn: a **confirmation** (yes/no), a **choice** ("the second one"), or a **slot** (a missing time, an AM/PM question, a title, or an adjustment to a defaulted time). If the reply is itself a complete new command, the pending step is dropped instead of absorbing it. |
| `classify` (**Conversational Agent**) | Intent classification and slot extraction into the `IntentResult` Pydantic schema. The LLM is tried first; its JSON is validated, and an invalid answer counts as a provider failure. If every provider fails, or none is configured, the deterministic rule parser (`rule_classify`) takes over, so core features work offline. |
| `reminder` (**Reminder Agent**) | Creates reminders, alarms, and meetings, plus list, cancel, snooze, and reschedule. Time expressions are resolved by `timeparse.py` in the user's IANA timezone. The LLM only copies the phrase verbatim and never computes dates. |
| `task` (**Task Management Agent**) | Creates, lists, completes, updates (due date, priority, rename), and deletes tasks. Tasks with a timed due date get a linked reminder, and rescheduling a task moves it. |
| `memory` (**Website Memory Agent**) | Saves pages and notes. Searches saved sites with token scoring plus a past-period filter ("yesterday", "last week"). Opens the best match. |
| `file` (**File Management Agent**) | Picks the file (the current PDF, or a named link on the page). Asks for confirmation on executable or archive types and large files. Stores metadata, then asks the extension to download it. |
| `page` (part of the Browser Context Agent) | Describes or summarises the page. Page text is wrapped in `<untrusted_page_content>` and the model is told to treat it as data. This path returns plain text only and **cannot call tools**, so injected instructions can't trigger actions. Without an LLM, an extractive summary is used. |
| `chat` | Help, greetings, and open-ended conversation. It never claims to have performed an action. |
| `respond` | Final shaping of the reply. `runner.py` then persists the messages and any new pending step. |

**Tools.** Agents never touch the database directly. They call `ToolBox.call(name, **args)`
(`agents/tools.py`). Each tool is a LangChain `StructuredTool` with a strict Pydantic args
schema, so every input is validated before it reaches a service function. A transient DB
error triggers a rollback and one retry. Every tool call is recorded in the turn's `trace`,
which the UI shows as step chips.

**Human confirmation.** Destructive or risky actions (cancelling a reminder, deleting a
task, downloading `.exe`/`.zip`/large files) return `needs_confirmation`. The agent stores
the exact target (an id or URL) in the pending step, so "yes" executes precisely what was
shown.

**Pending state** lives in `conversations.state` (JSON). Along with it NOVA stores a small
snapshot of the page the request was about (URL, title, type, file links), so "this
website" still resolves on the follow-up turn. Page *text* is never persisted.

## LLM provider fallback (`llm/providers.py`)

* `LLMRouter.complete()` tries providers in preference order (the server default
  `PREFERRED_PROVIDER`, or the user's choice in Settings).
* Timeouts and 5xx responses are retried with exponential backoff (`LLM_MAX_RETRIES`).
* A `429` or `RESOURCE_EXHAUSTED` puts that provider on a cooldown
  (`PROVIDER_COOLDOWN_SECONDS`) and switches to the next one immediately.
* Auth errors and unconfigured providers are skipped. Every failure is logged, and
  per-provider health is exposed at `GET /api/health/providers`.
* If every provider fails, the user gets a specific message (rate-limited / bad key /
  unavailable), and rule-based features keep working.

## Scheduling: why the extension owns time

A server can't show a Chrome notification while the browser is closed either. So the
backend is the source of truth and the extension owns timing:

1. After any change, the worker fetches pending reminders and stores a local mirror.
2. It creates one `chrome.alarms` entry per notification offset, named
   `nova:r:<id>:<offset>:<epoch>`. Meetings default to 10 minutes before plus on time.
3. On install, browser startup, and service-worker wake-up, `rebuildAlarms()` recreates
   missing alarms from the mirror (or the server). Reminders whose time passed while
   Chrome was closed are delivered at once as **missed**.
4. Notification buttons: *Open page* or *Mark done*, plus *Snooze 10 min*. The
   acknowledgement goes to the server, or to the offline outbox if the server is
   unreachable.

**Limitation (shown to users in Settings):** notifications appear only while Chrome is
running. If Chrome is closed or the computer is asleep, NOVA can't ring on time. It shows
missed reminders as soon as it can.

## Data model

| Table | Notes |
|---|---|
| `users` | `timezone` (IANA, kept current from the extension), `preferences` (JSONB) |
| `api_tokens` | Per-device bearer tokens. Only the SHA-256 hash is stored. |
| `saved_websites` | Unique `(user_id, url)`, `category`, `visit_count`, `last_visited_at` |
| `reminders` | `kind` reminder/alarm/meeting, `recurrence`, `notify_offsets`, `snoozed_until`, FK to task and website |
| `tasks` | `due_at` + `due_has_time` (date-only tasks are overdue only after their day ends), priority, recurrence |
| `saved_files` | Download status lifecycle `saved → requested → in_progress → complete/failed/interrupted`. One record per URL. |
| `conversations` / `messages` | Per-session history plus pending-step state |

All timestamps are stored in UTC (`UTCDateTime` keeps SQLite consistent with PostgreSQL's
`timestamptz`). Enumerated columns have CHECK constraints, every foreign key cascades or
nulls on delete, and list queries are covered by composite indexes such as
`(user_id, status, scheduled_at)`.

## Privacy and security

* **Default context** sent to the backend: URL, title, domain, and meta description of the
  active tab, plus links to files on the page. No page body.
* **Page text** is read only when the user approves (once per site, or "always" in
  Settings), and only for summarise/describe. Forms, inputs, text areas, and editable regions
  are stripped, so anything the user is typing never leaves the page.
  It's capped at 20k characters in the browser and `MAX_PAGE_CHARS` on the server, sent to
  the AI provider, and never stored.
* **Microphone** use starts only from an explicit click or shortcut. Recognition stops after
  one utterance, and nothing records in the background.
* **API keys** exist only in `backend/.env`. The extension holds a device token, and the
  server stores only its hash. `REGISTRATION_SECRET` gates sign-up for non-local servers.
* **Validation**: Pydantic validates every request and tool input. Only `http(s)` URLs ≤2048
  characters are accepted. Download folders are confined to `Downloads/NOVA/…` (no `..`,
  at most 3 levels), and filenames are sanitised.
* **Per-user isolation**: every service query filters by `user_id`, and a mismatched id
  returns 404.
* **Your data**: `GET /api/data/export` (JSON) and `DELETE /api/data?confirm=DELETE`, both
  available in Settings.
