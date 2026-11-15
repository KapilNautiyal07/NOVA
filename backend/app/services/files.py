from __future__ import annotations

import mimetypes
import posixpath
from datetime import datetime
from urllib.parse import unquote, urlparse

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import SavedFile, User
from ..schemas import FileIn, sanitize_filename
from .common import NotFound, search_tokens

# Types that could execute code or bundle unknown content → always confirm before downloading.
SENSITIVE_EXTENSIONS = {
    "exe", "msi", "bat", "cmd", "com", "scr", "ps1", "vbs", "js", "jar", "apk", "dmg", "pkg",
    "deb", "rpm", "sh", "app", "iso", "zip", "rar", "7z", "tar", "gz", "xz", "dll", "reg", "lnk",
}
LARGE_FILE_BYTES = 50 * 1024 * 1024
DOCUMENT_EXTENSIONS = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "csv", "txt", "md", "epub", "odt", "rtf"}
FILE_EXTENSIONS = DOCUMENT_EXTENSIONS | SENSITIVE_EXTENSIONS | {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "mp3", "mp4", "wav", "mov", "json", "xml", "ipynb",
}


def filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    name = posixpath.basename(path.rstrip("/")) or "download"
    return sanitize_filename(name)


def extension_of(name_or_url: str) -> str | None:
    path = urlparse(name_or_url).path if "://" in name_or_url else name_or_url
    base = posixpath.basename(path)
    if "." not in base:
        return None
    return base.rsplit(".", 1)[-1].lower()[:10]


def looks_like_file_url(url: str, content_type: str | None = None) -> bool:
    if content_type and not content_type.startswith("text/html"):
        return True
    ext = extension_of(url)
    return bool(ext and ext in FILE_EXTENSIONS)


def sensitivity(name: str, size_bytes: int | None) -> str | None:
    ext = extension_of(name)
    if ext in SENSITIVE_EXTENSIONS:
        return f"it's a .{ext} file, which can contain executable or bundled content"
    if size_bytes and size_bytes > LARGE_FILE_BYTES:
        return f"it's large ({size_bytes / (1024 * 1024):.0f} MB)"
    return None


def create_file(db: Session, user: User, data: FileIn) -> SavedFile:
    """Create file metadata — or reuse the record for a URL the user already saved, so asking
    twice (or "remember" then "download") doesn't produce duplicate entries."""
    existing = db.scalar(
        select(SavedFile)
        .where(SavedFile.user_id == user.id, SavedFile.file_url == data.file_url)
        .order_by(SavedFile.id.desc())
        .limit(1)
    )
    if existing is not None:
        if data.download:
            existing.status, existing.error = "requested", None
            existing.folder = data.folder or existing.folder
        if data.notes:
            existing.notes = data.notes
        existing.source_page_url = existing.source_page_url or data.source_page_url
        existing.source_page_title = existing.source_page_title or data.source_page_title
        db.commit()
        return existing
    name = sanitize_filename(data.file_name) if data.file_name else filename_from_url(data.file_url)
    if "." not in name:
        ext = mimetypes.guess_extension(data.file_type or "") or ""
        name = f"{name}{ext}"
    f = SavedFile(
        user_id=user.id,
        file_name=name,
        file_url=data.file_url,
        file_type=data.file_type or mimetypes.guess_type(name)[0],
        size_bytes=data.size_bytes,
        folder=data.folder,
        notes=data.notes,
        source_page_url=data.source_page_url,
        source_page_title=data.source_page_title,
        status="requested" if data.download else "saved",
    )
    db.add(f)
    db.commit()
    return f


def get_file(db: Session, user: User, file_id: int) -> SavedFile:
    f = db.get(SavedFile, file_id)
    if not f or f.user_id != user.id:
        raise NotFound("file", file_id)
    return f


def update_file(db: Session, user: User, file_id: int, **fields) -> SavedFile:
    f = get_file(db, user, file_id)
    for k, v in fields.items():
        if v is not None:
            setattr(f, k, v)
    if fields.get("status") == "complete":
        f.error = None
    db.commit()
    return f


def search_files(
    db: Session,
    user: User,
    query: str = "",
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 50,
) -> list[SavedFile]:
    stmt = select(SavedFile).where(SavedFile.user_id == user.id)
    if start and end:
        stmt = stmt.where(and_(SavedFile.created_at >= start, SavedFile.created_at < end))
    tokens = search_tokens(query)
    if tokens:
        conds = []
        for tok in tokens:
            like = f"%{tok}%"
            conds += [
                func.lower(SavedFile.file_name).like(like),
                func.lower(func.coalesce(SavedFile.notes, "")).like(like),
                func.lower(func.coalesce(SavedFile.source_page_title, "")).like(like),
                func.lower(func.coalesce(SavedFile.folder, "")).like(like),
                func.lower(func.coalesce(SavedFile.file_type, "")).like(like),
            ]
        stmt = stmt.where(or_(*conds))
    return list(db.scalars(stmt.order_by(SavedFile.created_at.desc()).limit(limit)))


def delete_file(db: Session, user: User, file_id: int) -> None:
    db.delete(get_file(db, user, file_id))
    db.commit()
