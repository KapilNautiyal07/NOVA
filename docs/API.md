# NOVA REST API

Base URL: `http://localhost:8000` (configurable in the extension's Settings).
Interactive docs (Swagger UI): **`/docs`**. The OpenAPI schema is at `/openapi.json`.

Every endpoint except `/api/health` and `/api/auth/register` needs
`Authorization: Bearer <device token>`. Datetimes are ISO 8601 and **must include an
offset**. Responses are always in UTC.

## Auth and account

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/auth/register` | `{name, email, timezone, device_name?, registration_secret?}` → `201 {user, token}`. The token is shown once. `403` if `REGISTRATION_SECRET` doesn't match, `409` if the email exists, `429` if registrations are rate-limited. |
| `GET` | `/api/auth/me` | Current user |
| `PATCH` | `/api/auth/me` | `{name?, timezone?, preferences?}`. For example `preferences.provider = "groq"` makes Groq the first provider tried. |
| `POST` | `/api/auth/tokens?device_name=…` | Issue another device token for the signed-in user |

## Assistant

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/assistant/message` | Runs one agent turn (rate-limited per user) |
| `POST` | `/api/assistant/suggest` | `{page: {url, title, …}}` → a contextual suggestion for the page type |
| `GET` | `/api/conversations/{session_id}/messages` | Conversation history |
| `POST` | `/api/voice/transcribe` | multipart `audio` (≤ `MAX_AUDIO_BYTES`) → `{text, provider}` via Gemini (Groq fallback) — see [Voice](#voice) |
| `GET` | `/api/health/providers` | Per-provider configured / success / failure / cooldown status |

`POST /api/assistant/message`:

```json
{
  "session_id": "tab-42",
  "message": "Remind me to watch this video tomorrow at 8 PM",
  "timezone": "Asia/Kolkata",
  "page": {
    "url": "https://www.youtube.com/watch?v=abc",
    "title": "LangGraph Crash Course",
    "description": "…",
    "content_type": "text/html",
    "selection": null,
    "text": null,
    "file_links": [{"url": "https://…/report.pdf", "text": "Report", "ext": "pdf"}]
  },
  "page_text_allowed": false
}
```

Response:

```json
{
  "reply": "I've saved this website. Done! I'll remind you tomorrow at 8:00 PM: Watch “LangGraph Crash Course”.",
  "status": "done",
  "intent": "create_reminder",
  "agent": "reminder",
  "client_actions": [{"type": "sync_reminders", "payload": {}}],
  "data": {"reminder": {"id": 7, "scheduled_at": "2026-11-10T14:30:00Z", "…": "…"}},
  "trace": [{"node": "browser_context", "detail": "video · youtube.com", "ok": true}, "…"],
  "provider": "gemini",
  "suggestions": []
}
```

`status` is one of `done`, `needs_clarification`, `needs_confirmation`,
`needs_permission` (send again with `page.text` and `page_text_allowed: true`), or `error`.

The extension executes these `client_actions`:

| Type | Payload | Extension behaviour |
|---|---|---|
| `sync_reminders` | `{}` | Re-fetch pending reminders and reconcile `chrome.alarms` |
| `open_url` | `{url}` | Open a tab |
| `download` | `{file_id, url, filename}` | `chrome.downloads.download`, then report progress via `PATCH /api/files/{id}/status` |
| `show_view` | `{view}` | Switch the dashboard view |
| `request_page_content` | `{reason}` | Ask permission, extract page text, and resend |

## Reminders

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/reminders?status=pending&status=snoozed&kind=alarm&limit=100` | List |
| `POST` | `/api/reminders` | `{title, scheduled_at, timezone, kind?, recurrence?, notify_offsets?, related_url?, related_title?, task_id?, description?}` |
| `GET` | `/api/reminders/{id}` | One reminder |
| `PATCH` | `/api/reminders/{id}` | Edit. A new `scheduled_at` re-arms a fired or snoozed reminder. |
| `POST` | `/api/reminders/{id}/snooze` | `{minutes}` (1–10080, default 10) |
| `POST` | `/api/reminders/{id}/complete` | Recurring reminders advance to the next occurrence |
| `POST` | `/api/reminders/{id}/fired` | Sent by the extension after notifying. Recurring reminders advance. |
| `DELETE` | `/api/reminders/{id}` | `204` |

`kind`: `reminder | alarm | meeting`. `recurrence`: `daily | weekdays | weekly | monthly`.
`status`: `pending | snoozed | fired | completed | cancelled`.

## Tasks

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/tasks?status=pending&priority=high&due=today&q=report` | `due`: `today | tomorrow | upcoming | overdue | none` |
| `POST` | `/api/tasks` | `{title, description?, due_at?, due_has_time?, priority?, recurrence?, related_url?, related_title?}` |
| `GET` | `/api/tasks/{id}` | One task |
| `PATCH` | `/api/tasks/{id}` | Edit. Changing `due_at` moves the task's reminders, and renaming renames them. |
| `POST` | `/api/tasks/{id}/complete` | Recurring tasks spawn the next occurrence |
| `DELETE` | `/api/tasks/{id}` | `204`. Its pending reminders are cancelled. |

## Websites

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/websites?q=langgraph&period=last week&category=docs` | Search saved sites |
| `POST` | `/api/websites` | `{url, title?, notes?, category?}`. Upserts by URL. |
| `GET` | `/api/websites/{id}` | One site |
| `PATCH` | `/api/websites/{id}` | `{title?, notes?, category?}` |
| `POST` | `/api/websites/visit` | `{url, title?}`. Bumps `last_visited_at` **only if the URL is already saved** (NOVA doesn't log general browsing). |
| `DELETE` | `/api/websites/{id}` | `204` |

## Files

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/files?q=report&period=yesterday` | Search file metadata |
| `POST` | `/api/files` | `{file_url, file_name?, folder?, notes?, download?}`. Reuses the existing record for the same URL. |
| `GET` | `/api/files/{id}` | One file |
| `PATCH` | `/api/files/{id}` | `{notes?, folder?}` |
| `PATCH` | `/api/files/{id}/status` | Sent by the extension: `{status, local_download_id?, size_bytes?, local_path?, error?}` |
| `DELETE` | `/api/files/{id}` | Removes metadata only. The downloaded file stays on disk. |

## Your data

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/data/export` | Everything stored for you, as JSON |
| `DELETE` | `/api/data?confirm=DELETE[&delete_account=true]` | Erase reminders, tasks, websites, files, and conversations (optionally the account too) |

## Errors

`401` missing or invalid token · `403` bad registration secret · `404` not found (including
another user's ids) · `409` email exists · `422` validation error (FastAPI detail format) ·
`429` rate-limited.

## Voice

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/voice/transcribe` | multipart `audio` (WAV/WebM/OGG…, ≤ 10 MB), optional `preview=true` for live captions | `{text, provider: "gemini"\|"groq", fallback_from: ["gemini:rate_limit", …]}`. 503 if no STT provider is configured. |
| POST | `/api/voice/speak` | `{text, voice?}` (one of the 30 prebuilt Gemini voices) | `{audio: base64 WAV (24 kHz mono), mime, spoken_text}`. `spoken_text` is the reply with markdown, links and emoji removed and long answers shortened. 503/502 → the client uses the browser voice. |
| GET | `/api/voice/config` | — | `{stt: [...available providers], tts: bool, voices: [...], default_voice}` |

`POST /api/assistant/message` also accepts `channel: "voice"`. Replies are then kept short
and written to be heard (no lists or markdown).

## Google Calendar

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/api/integrations/google/connect` | token | `{auth_url, attempt_id}`. Open `auth_url` only after an explicit user click. 503 if the server has no OAuth client. |
| GET | `/api/integrations/google/callback` | none (Google redirect; the single-use `state` identifies the attempt) | HTML page: connected, not connected (denied, or calendar scope unticked), error, or expired. `Cache-Control: no-store`. |
| GET | `/api/integrations/google/status?attempt=ID` | token | `{configured, connected, account, scopes, attempt: {outcome: pending\|connected\|denied\|error\|expired, detail}}`. Tokens are never returned. |
| DELETE | `/api/integrations/google` | token | Revokes the grant at Google and deletes the stored tokens. |

Assistant responses for scheduling use `status: "needs_authorization"` with a
`connect_calendar` client action (show a Connect button; with `payload.auto: true`, sent after
the user said "yes", the client opens Google's consent page right away), `needs_clarification`
when the time is missing (title and length default to "Meeting" and `CALENDAR_DEFAULT_MINUTES`),
`needs_confirmation` with `data.event_draft` before creating, and a `show_link` client action
pointing to the created event. When Google Calendar isn't configured on the server, the same
request sets a meeting reminder instead (`intent` stays `schedule_event`).
