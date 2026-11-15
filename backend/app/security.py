"""Bearer-token auth and rate limiting."""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db, utcnow
from .models import ApiToken, User

log = logging.getLogger("nova.security")
bearer = HTTPBearer(auto_error=False)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(db: Session, user: User, name: str) -> str:
    token = "nova_" + secrets.token_urlsafe(32)
    db.add(ApiToken(user_id=user.id, token_hash=hash_token(token), name=name[:80]))
    db.commit()
    return token


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)
) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    tok = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(creds.credentials)))
    if tok is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
    now = utcnow()
    if tok.last_used_at is None or (now - tok.last_used_at).total_seconds() > 300:
        tok.last_used_at = now
        db.commit()
    return tok.user


class RateLimiter:
    """Fixed-window limiter backed by Redis when available, otherwise in-process."""

    def __init__(self, per_minute: int, redis_url: str | None):
        self.per_minute = per_minute
        self._redis = None
        self._local: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()
        if redis_url:
            try:
                import redis

                client = redis.Redis.from_url(redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
                client.ping()
                self._redis = client
                log.info("Rate limiter using Redis")
            except Exception as exc:  # pragma: no cover - depends on environment
                log.warning("Redis unavailable (%s); using in-process rate limiter", exc)

    def hit(self, key: str) -> bool:
        if self._redis is not None:
            try:
                bucket = f"nova:rl:{key}:{int(time.time() // 60)}"
                count = self._redis.incr(bucket)
                if count == 1:
                    self._redis.expire(bucket, 65)
                return count <= self.per_minute
            except Exception:  # pragma: no cover
                pass
        now = time.monotonic()
        with self._lock:
            q = self._local[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        s = get_settings()
        _limiter = RateLimiter(s.rate_limit_per_minute, s.redis_url)
    return _limiter


def rate_limited(request: Request, user: User = Depends(current_user)) -> User:
    if not get_limiter().hit(f"user:{user.id}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests — slow down a little.")
    return user
