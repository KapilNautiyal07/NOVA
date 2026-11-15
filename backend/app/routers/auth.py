from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import User
from ..schemas import RegisterIn, RegisterOut, UserOut, UserUpdate
from ..security import current_user, get_limiter, issue_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=RegisterOut, status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    """Create an account and return a device token (shown once; only its hash is stored)."""
    settings = get_settings()
    if not get_limiter().hit("register"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many registrations")
    if settings.registration_secret and not hmac.compare_digest(
        body.registration_secret or "", settings.registration_secret
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid registration secret")
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This email is already registered. Use your existing token, or create a new device token from a signed-in device.",
        )
    user = User(name=body.name.strip(), email=email, timezone=body.timezone, preferences={})
    db.add(user)
    db.commit()
    token = issue_token(db, user, body.device_name)
    return RegisterOut(user=UserOut.model_validate(user), token=token)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


@router.patch("/me", response_model=UserOut)
def update_me(body: UserUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.name is not None:
        user.name = body.name
    if body.timezone is not None:
        user.timezone = body.timezone
    if body.preferences is not None:
        user.preferences = {**(user.preferences or {}), **body.preferences}
    db.commit()
    return user


@router.post("/tokens", status_code=201)
def new_token(device_name: str = "device", user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"token": issue_token(db, user, device_name)}
