"""File Management Agent: browser downloads + file metadata memory.

NOVA can only download into the browser's Downloads directory (sub-folders allowed) via
chrome.downloads; it never reads or moves arbitrary local files.
"""
from __future__ import annotations

from ..schemas import sanitize_folder
from ..services.common import search_tokens
from ..services.files import extension_of, filename_from_url, looks_like_file_url, sensitivity
from ..timeparse import humanize, parse_period
from .base import AgentInput, ask_choice, ask_confirm, quote, result, ser_file

TYPE_EXTS = {
    "pdf": {"pdf"}, "image": {"png", "jpg", "jpeg", "gif", "webp", "svg"}, "zip": {"zip", "rar", "7z"},
    "doc": {"doc", "docx", "odt", "rtf", "pdf"}, "slides": {"ppt", "pptx", "key", "pdf"},
    "spreadsheet": {"xls", "xlsx", "csv"},
}


def _candidates(inp: AgentInput) -> list[dict]:
    page = inp.page
    if not page.get("url"):
        return []
    wanted = TYPE_EXTS.get(inp.intent.file_type or "")
    if looks_like_file_url(page["url"], page.get("content_type") if page.get("content_type") not in (None, "text/html") else None):
        ext = extension_of(page["url"]) or ("pdf" if page.get("page_type") == "pdf" else None)
        if not wanted or (ext and ext in wanted) or page.get("page_type") == "pdf":
            return [{"url": page["url"], "label": filename_from_url(page["url"]), "type": page.get("content_type")}]
    links = page.get("file_links") or []
    out = []
    for link in links:
        ext = link.get("ext") or extension_of(link["url"])
        if wanted and ext not in wanted:
            continue
        label = (link.get("text") or "").strip() or filename_from_url(link["url"])
        out.append({"url": link["url"], "label": label[:80]})
    # "Download the installer" → prefer links whose text/URL mention what was asked for.
    tokens = search_tokens(inp.intent.query or "")
    if tokens and len(out) > 1:
        named = [c for c in out if any(t in f"{c['label']} {c['url']}".lower() for t in tokens)]
        out = named or out
    return out


def _pick(inp: AgentInput, verb: str) -> tuple[dict | None, dict | None]:
    if inp.extra.get("chosen_url"):
        return {"url": inp.extra["chosen_url"], "label": inp.extra.get("chosen_label", "")}, None
    if inp.extra.get("chosen_id"):
        opt = next((o for o in inp.extra.get("options", []) if o["id"] == inp.extra["chosen_id"]), None)
        if opt:
            return {"url": opt["url"], "label": opt["label"]}, None
    if not inp.page.get("url"):
        msg = "I can't access files on this page." if inp.page.get("restricted") else "Open the page with the file first."
        return None, result(msg, status="error")
    cands = _candidates(inp)
    kind = inp.intent.file_type.upper() if inp.intent.file_type == "pdf" else (inp.intent.file_type or "downloadable file")
    if not cands:
        return None, result(f"I couldn't find a {kind} on this page.", status="error")
    if len(cands) > 1:
        opts = [{"id": i + 1, "label": c["label"], "url": c["url"]} for i, c in enumerate(cands[:6])]
        return None, ask_choice(f"I found {len(cands)} files on this page. Which one should I {verb}?", inp, opts, options=opts)
    return cands[0], None


def download(inp: AgentInput) -> dict:
    target, clar = _pick(inp, "download")
    if clar:
        return clar
    page = inp.page
    try:
        folder = sanitize_folder(inp.intent.folder)
    except ValueError:
        return result("That folder name isn't allowed. Use letters, numbers, spaces, - or _.", status="error")
    name = filename_from_url(target["url"])
    size = next((l.get("size") for l in page.get("file_links") or [] if l["url"] == target["url"]), None)
    risk = sensitivity(name, size)
    if risk and not inp.confirmed:
        return ask_confirm(f"Download {quote(name)}? Heads up: {risk}.", inp, chosen_url=target["url"], chosen_label=target.get("label"))
    rec = inp.tools.call(
        "create_file_record",
        file_url=target["url"],
        file_name=name,
        folder=folder,
        source_page_url=page.get("url"),
        source_page_title=page.get("title"),
        file_type=page.get("content_type") if target["url"] == page.get("url") and page.get("content_type") != "text/html" else None,
        download=True,
    )
    rel = f"NOVA/{folder + '/' if folder else ''}{rec.file_name}"
    return result(
        f"Downloading {quote(rec.file_name)} to Downloads/{rel.rsplit('/', 1)[0]}. I've saved its details too.",
        actions=[{"type": "download", "payload": {"file_id": rec.id, "url": rec.file_url, "filename": rel}}],
        data={"file": ser_file(rec)},
    )


def remember(inp: AgentInput) -> dict:
    target, clar = _pick(inp, "remember")
    if clar:
        return clar
    page = inp.page
    rec = inp.tools.call(
        "create_file_record",
        file_url=target["url"],
        file_name=filename_from_url(target["url"]),
        source_page_url=page.get("url"),
        source_page_title=page.get("title"),
        notes=inp.intent.notes,
        download=False,
    )
    return result(f"I'll remember {quote(rec.file_name)}. Say \"download it\" any time to save a copy.",
                  data={"file": ser_file(rec)})


def search(inp: AgentInput) -> dict:
    now = inp.deps.now
    period = parse_period(inp.intent.period or inp.message, now)
    start, end, label = period if period else (None, None, None)
    files = inp.tools.call("search_files", query=inp.intent.query or "", start=start, end=end, limit=20)
    if not files and inp.intent.query and period:
        files = inp.tools.call("search_files", start=start, end=end, limit=20)  # query too strict → period only
    scope = f" from {label}" if label else ""
    if not files:
        return result(f"I couldn't find any saved files{scope}.", data={"files": []},
                      actions=[{"type": "show_view", "payload": {"view": "files"}}])
    lines = [f"• {f.file_name} — {f.status}, {humanize(f.created_at, now)}" for f in files[:5]]
    return result(f"I found {len(files)} file{'s' if len(files) != 1 else ''}{scope}:\n" + "\n".join(lines),
                  data={"files": [ser_file(f) for f in files]},
                  actions=[{"type": "show_view", "payload": {"view": "files"}}])


def run(inp: AgentInput) -> dict:
    return {"download_file": download, "remember_file": remember, "search_files": search}[inp.intent.intent](inp)
