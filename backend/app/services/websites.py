from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import SavedWebsite, User
from .common import NotFound, categorize, domain_of, search_tokens


def save_website(
    db: Session, user: User, url: str, title: str = "", notes: str | None = None, category: str | None = None
) -> tuple[SavedWebsite, bool]:
    """Upsert a saved website. Returns (website, created)."""
    site = db.scalar(select(SavedWebsite).where(SavedWebsite.user_id == user.id, SavedWebsite.url == url))
    now = utcnow()
    if site:
        if title:
            site.title = title[:500]
        if notes:
            site.notes = f"{site.notes}\n{notes}" if site.notes and notes not in site.notes else notes
        if category:
            site.category = category
        site.last_visited_at = now
        site.visit_count += 1
        db.commit()
        return site, False
    site = SavedWebsite(
        user_id=user.id,
        url=url,
        title=(title or domain_of(url))[:500],
        domain=domain_of(url),
        notes=notes,
        category=category or categorize(url),
        created_at=now,
        last_visited_at=now,
    )
    db.add(site)
    db.commit()
    return site, True


def get_website(db: Session, user: User, website_id: int) -> SavedWebsite:
    site = db.get(SavedWebsite, website_id)
    if not site or site.user_id != user.id:
        raise NotFound("website", website_id)
    return site


def record_visit(db: Session, user: User, url: str, title: str | None = None) -> SavedWebsite | None:
    """Only already-saved websites get visit tracking — NOVA never logs general browsing."""
    site = db.scalar(select(SavedWebsite).where(SavedWebsite.user_id == user.id, SavedWebsite.url == url))
    if not site:
        return None
    site.last_visited_at = utcnow()
    site.visit_count += 1
    if title:
        site.title = title[:500]
    db.commit()
    return site


def list_websites(
    db: Session, user: User, category: str | None = None, limit: int = 50, offset: int = 0
) -> list[SavedWebsite]:
    stmt = select(SavedWebsite).where(SavedWebsite.user_id == user.id)
    if category:
        stmt = stmt.where(SavedWebsite.category == category)
    stmt = stmt.order_by(SavedWebsite.last_visited_at.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt))


def search_websites(
    db: Session,
    user: User,
    query: str = "",
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 20,
) -> list[SavedWebsite]:
    """Keyword search over the user's saved websites, optionally within a time period.

    A site matches a period if it was saved OR last visited inside it.
    """
    tokens = search_tokens(query)
    stmt = select(SavedWebsite).where(SavedWebsite.user_id == user.id)
    if start and end:
        stmt = stmt.where(
            or_(
                and_(SavedWebsite.created_at >= start, SavedWebsite.created_at < end),
                and_(SavedWebsite.last_visited_at >= start, SavedWebsite.last_visited_at < end),
            )
        )
    if tokens:
        conds = []
        for tok in tokens:
            like = f"%{tok}%"
            conds += [
                func.lower(SavedWebsite.title).like(like),
                func.lower(SavedWebsite.url).like(like),
                func.lower(func.coalesce(SavedWebsite.notes, "")).like(like),
                func.lower(func.coalesce(SavedWebsite.category, "")).like(like),
            ]
        stmt = stmt.where(or_(*conds))
    candidates = list(db.scalars(stmt.order_by(SavedWebsite.last_visited_at.desc()).limit(500)))
    if not tokens:
        return candidates[:limit]

    def score(site: SavedWebsite) -> float:
        s = 0.0
        title, url, notes = site.title.lower(), site.url.lower(), (site.notes or "").lower()
        for tok in tokens:
            s += 3 * (tok in title) + 2 * (tok in notes) + 1.5 * (tok in site.domain) + 1 * (tok in url)
            s += 1 * (tok == (site.category or "").lower())
        return s

    ranked = sorted(candidates, key=lambda x: (score(x), x.last_visited_at), reverse=True)
    return ranked[:limit]


def update_website(db: Session, user: User, website_id: int, **fields) -> SavedWebsite:
    site = get_website(db, user, website_id)
    for k, v in fields.items():
        if v is not None:
            setattr(site, k, v)
    db.commit()
    return site


def delete_website(db: Session, user: User, website_id: int) -> None:
    site = get_website(db, user, website_id)
    db.delete(site)
    db.commit()
