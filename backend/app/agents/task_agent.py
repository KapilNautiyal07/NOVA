"""Task Management Agent: create / list / complete / delete tasks."""
from __future__ import annotations

from ..services.tasks import is_overdue
from ..timeparse import humanize, humanize_date, parse_recurrence
from .base import AgentInput, ask_choice, ask_confirm, ask_slot, quote, result, ser_reminder, ser_task
from .reminder_agent import build_title, resolve_when



def create(inp: AgentInput) -> dict:
    it = inp.intent
    parsed = resolve_when(inp)
    title = build_title(inp, parsed, "")
    if not title:
        return ask_slot("What should the task be called?", inp, "title")
    page = inp.page
    uses_page = bool(page.get("url")) and it.use_current_page
    recurrence = it.recurrence or parse_recurrence(inp.message)
    task = inp.tools.call(
        "create_task",
        title=title,
        due_at=parsed.utc if parsed else None,
        due_has_time=bool(parsed and parsed.has_time),
        priority=it.priority or "medium",
        recurrence=recurrence,
        related_url=page.get("url") if uses_page else None,
        related_title=page.get("title") if uses_page else None,
    )
    data = {"task": ser_task(task)}
    actions = []
    if parsed and parsed.has_time and parsed.dt > inp.deps.now:
        rem = inp.tools.call(
            "create_reminder",
            title=title,
            scheduled_at=parsed.utc,
            timezone=inp.deps.user.timezone,
            recurrence=recurrence,
            task_id=task.id,
            related_url=task.related_url,
            related_title=task.related_title,
        )
        data["reminder"] = ser_reminder(rem)
        actions.append({"type": "sync_reminders", "payload": {}})
        due = f", due {humanize(parsed.dt, inp.deps.now)} — I'll remind you then"
    elif parsed:
        due = f", due {humanize_date(parsed.dt, inp.deps.now)}"
    else:
        due = ""
    prio = f" [{task.priority} priority]" if task.priority != "medium" else ""
    return result(f"Task created: {quote(title)}{due}{prio}.", data=data, actions=actions)


def list_(inp: AgentInput) -> dict:
    it = inp.intent
    statuses = {"completed": ["completed"], "all": None}.get(it.status_filter or "open", ["pending", "in_progress"])
    due = it.due_filter if it.due_filter in ("today", "tomorrow", "upcoming", "overdue") else None
    tasks = inp.tools.call("list_tasks", statuses=statuses, priority=it.priority, due=due, limit=100)
    label = {"completed": "completed", "all": ""}.get(it.status_filter or "open", "pending")
    scope = {"today": " for today", "tomorrow": " for tomorrow", "upcoming": " coming up", "overdue": " that are overdue"}.get(due or "", "")
    prio = f" {it.priority}-priority" if it.priority else ""
    actions = [{"type": "show_view", "payload": {"view": "tasks"}}]
    if not tasks:
        return result(f"You have no{prio} {label} tasks{scope}. 🎉".replace("  ", " "), data={"tasks": []}, actions=actions)
    now = inp.deps.now
    lines = []
    for t in tasks[:6]:
        due_s = f" — due {humanize(t.due_at, now) if t.due_has_time else humanize_date(t.due_at, now)}" if t.due_at else ""
        overdue = " (overdue)" if is_overdue(t, now) else ""
        badge = f" [{t.priority}]" if t.priority in ("high", "urgent") else ""
        lines.append(f"• {t.title}{badge}{due_s}{overdue}")
    more = f"\n…and {len(tasks) - 6} more." if len(tasks) > 6 else ""
    head = f"You have {len(tasks)}{prio} {label} task{'s' if len(tasks) != 1 else ''}{scope}:".replace("  ", " ")
    return result(head + "\n" + "\n".join(lines) + more, data={"tasks": [ser_task(t) for t in tasks]}, actions=actions)


def _target(inp: AgentInput, verb: str):
    chosen = inp.extra.get("chosen_id")
    if chosen:
        hits = [t for t in inp.tools.call("list_tasks", limit=500) if t.id == chosen]
        return (hits[0], None) if hits else (None, result("That task no longer exists.", status="error"))
    # For update_task, `title` is the task's *new* name, so only `reference` identifies it.
    ref = inp.intent.reference or (inp.intent.title if inp.intent.intent != "update_task" else None) or ""
    hits = inp.tools.call("find_tasks", reference=ref) if ref else []
    if not hits:
        open_tasks = inp.tools.call("list_tasks", statuses=["pending", "in_progress"], limit=5)
        if not open_tasks:
            return None, result("You don't have any open tasks.", data={"tasks": []})
        opts = [{"id": t.id, "label": t.title} for t in open_tasks]
        prefix = f"I couldn't find a task matching {quote(ref)}. " if ref else ""
        return None, ask_choice(f"{prefix}Which task should I {verb}?", inp, opts)
    if len(hits) > 1:
        return None, ask_choice(f"Which task should I {verb}?", inp, [{"id": t.id, "label": t.title} for t in hits[:5]])
    return hits[0], None


def complete(inp: AgentInput) -> dict:
    task, clar = _target(inp, "mark as done")
    if clar:
        return clar
    task, nxt = inp.tools.call("complete_task", id=task.id)
    reply = f"Nice work! Marked {quote(task.title)} as done."
    data = {"task": ser_task(task)}
    if nxt:
        reply += f" Next occurrence: {humanize_date(nxt.due_at, inp.deps.now)}."
        data["next_task"] = ser_task(nxt)
    return result(reply, data=data, actions=[{"type": "sync_reminders", "payload": {}}])


def delete(inp: AgentInput) -> dict:
    task, clar = _target(inp, "delete")
    if clar:
        return clar
    if not inp.confirmed:
        return ask_confirm(f"Delete the task {quote(task.title)}? This can't be undone.", inp, chosen_id=task.id)
    title = task.title
    inp.tools.call("delete_task", id=task.id)
    return result(f"Deleted the task {quote(title)}.", actions=[{"type": "sync_reminders", "payload": {}}])


def update(inp: AgentInput) -> dict:
    task, clar = _target(inp, "change")
    if clar:
        return clar
    it, now = inp.intent, inp.deps.now
    changes: dict = {}
    said: list[str] = []
    parsed = None
    if it.when or it.datetime_iso:
        parsed = resolve_when(inp, use_message=False)
        if parsed is None:
            return ask_slot(f"When should {quote(task.title)} be due?", inp, "when", chosen_id=task.id)
        if parsed.has_time and parsed.dt <= now:
            return ask_slot(f"{humanize(parsed.dt, now).capitalize()} has already passed. When should it be due instead?",
                            inp, "when", chosen_id=task.id)
        changes.update(due_at=parsed.utc, due_has_time=parsed.has_time)
        said.append(f"due {humanize(parsed.dt, now) if parsed.has_time else humanize_date(parsed.dt, now)}")
    if it.priority and it.priority != task.priority:
        changes["priority"] = it.priority
        said.append(f"{it.priority} priority")
    if it.title and it.title.strip() and it.title.strip() != task.title:
        changes["title"] = it.title.strip()[:300]
        said.append(f"renamed to {quote(changes['title'])}")
    if not changes:
        return result(f"What should I change about {quote(task.title)}? You can say a new due date, a priority, "
                      f"or \"rename it to …\".", status="needs_clarification")
    old_title = task.title
    task = inp.tools.call("update_task", id=task.id, **changes)
    data = {"task": ser_task(task)}
    actions = [{"type": "sync_reminders", "payload": {}}]
    has_reminder = any(r.status in ("pending", "snoozed") for r in task.reminders)
    if parsed and parsed.has_time and not has_reminder:
        rem = inp.tools.call("create_reminder", title=task.title, scheduled_at=parsed.utc, timezone=inp.deps.user.timezone,
                             recurrence=task.recurrence, task_id=task.id, related_url=task.related_url,
                             related_title=task.related_title)
        data["reminder"] = ser_reminder(rem)
        said.append("I'll remind you then")
    return result(f"Updated {quote(old_title)}: " + ", ".join(said) + ".", data=data, actions=actions)


def run(inp: AgentInput) -> dict:
    return {"create_task": create, "list_tasks": list_, "complete_task": complete, "delete_task": delete,
            "update_task": update}[inp.intent.intent](inp)
