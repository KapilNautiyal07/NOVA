"""Google Calendar connection endpoints.

  POST   /api/integrations/google/connect   (auth)  → consent URL for an explicit user click
  GET    /api/integrations/google/callback  (Google redirects the browser here; no NOVA token —
                                             the single-use `state` identifies the attempt)
  GET    /api/integrations/google/status    (auth)  → connected? + outcome of an attempt
  DELETE /api/integrations/google           (auth)  → revoke at Google and delete tokens
"""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..integrations import google_calendar as gcal
from ..models import User
from ..security import current_user, rate_limited

router = APIRouter(prefix="/api/integrations/google", tags=["integrations"])


@router.post("/connect")
def connect(user: User = Depends(rate_limited), db: Session = Depends(get_db)):
    try:
        return gcal.start_authorization(db, user)
    except gcal.CalendarNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/status")
def status(attempt: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return gcal.status(db, user, attempt)


@router.delete("")
def disconnect(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"disconnected": gcal.disconnect(db, user)}


_PAGES = {
    "connected": ("Google Calendar connected", "NOVA can now add events to your calendar — it will always confirm the details with you first. You can close this tab and go back to NOVA."),
    "denied": ("Not connected", "You didn't give NOVA access to your calendar, so nothing was connected. You can close this tab. You can connect later from NOVA's settings."),
    "error": ("Couldn't connect", "Something went wrong while connecting Google Calendar. Close this tab and try again from NOVA."),
    "invalid": ("Link expired", "This sign-in link has expired or was already used. Start again from NOVA."),
}


@router.get("/callback", response_class=HTMLResponse, include_in_schema=False)
def callback(state: str | None = None, code: str | None = None, error: str | None = None, scope: str | None = None,
             db: Session = Depends(get_db)):
    res = gcal.handle_callback(db, state, code, error, scope)
    title, body = _PAGES.get(res.outcome, _PAGES["error"])
    if res.outcome == "denied" and res.detail == "scope_not_granted":
        body = "Calendar access wasn't ticked on Google's consent screen, so NOVA can't add events. Nothing was stored. Try again and allow “See, edit, share, and permanently delete events” if you'd like NOVA to schedule meetings."
    ok = res.outcome == "connected"
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NOVA · {html.escape(title)}</title><meta name="referrer" content="no-referrer">
<style>body{{font:16px/1.5 system-ui,sans-serif;background:#0b0b12;color:#e8e8f0;display:grid;place-items:center;min-height:100vh;margin:0}}
main{{max-width:440px;padding:32px;border-radius:16px;background:#151522;border:1px solid #2a2a3d;text-align:center}}
.i{{font-size:40px;color:{'#34d399' if ok else '#f59e0b'}}}h1{{font-size:20px;margin:8px 0}}p{{color:#a8a8bd}}</style></head>
<body><main><div class="i">{'✓' if ok else '!'}</div><h1>{html.escape(title)}</h1><p>{html.escape(body)}</p></main></body></html>"""
    # no-store: never cache a page reached with an auth code in its URL
    return HTMLResponse(page, status_code=200 if res.outcome != "invalid" else 400, headers={"Cache-Control": "no-store"})
