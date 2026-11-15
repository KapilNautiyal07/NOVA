"""Deterministic natural-language date/time resolution.

The LLM extracts the *time expression* ("tomorrow at 8 PM"); this module turns it into an
aware datetime in the user's timezone. Keeping this deterministic makes scheduling testable
and immune to model hallucinations about the current date.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "couple": 2,
    "a couple of": 2, "couple of": 2, "few": 3, "a few": 3,
}
_NUM = r"(\d+(?:\.\d+)?|a couple of|couple of|a few|few|forty-five|" + "|".join(
    sorted((k for k in NUMBER_WORDS if " " not in k and k != "forty-five"), key=len, reverse=True)
) + r")"

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "tues": 1, "wednesday": 2, "thursday": 3, "thurs": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
MONTHS["sept"] = 9
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))

PART_OF_DAY = {"morning": 9, "afternoon": 15, "evening": 19, "night": 21, "tonight": 20}

_UNIT_RE = r"(minutes?|mins?|hours?|hrs?|days?|weeks?|months?)"
_REL_RE = re.compile(rf"\b(?:in|after|within)\s+{_NUM}\s*{_UNIT_RE}\b(?:\s+from\s+now)?")
_REL_FROM_NOW_RE = re.compile(rf"\b{_NUM}\s*{_UNIT_RE}\s+(?:from\s+now|later)\b")
_HALF_HOUR_RE = re.compile(r"\b(?:in|after)\s+(?:half\s+an?\s+hour|30\s*mins?)\b")
_TIME_AMPM_RE = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])")
_TIME_24_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_TIME_AT_RE = re.compile(
    r"\b(?:at|@|by|around)\s+(\d{1,2})(?:[:.](\d{2}))?\b(?!\s*(?:min|hour|hr|day|week|month|%|/|th|st|nd|rd))"
)
_NOON_RE = re.compile(r"\b(noon|midday|midnight)\b")
_POD_RE = re.compile(r"\b(?:this\s+|in\s+the\s+|tomorrow\s+)?(morning|afternoon|evening|night|tonight)\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DM_RE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\.?(?:,?\s+(\d{{4}}))?\b")
_MD_RE = re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b")
_WEEKDAY_RE = re.compile(
    r"\b(?:(next|this|coming|on)\s+)?(monday|tuesday|tues|wednesday|thursday|thurs|friday|saturday|sunday)\b"
)
_DAY_AFTER_RE = re.compile(r"\b(?:the\s+)?day\s+after\s+tomorrow\b")
_TOMORROW_RE = re.compile(r"\b(tomorrow|tmrw|tmr)\b")
_TODAY_RE = re.compile(r"\b(today|tonight)\b")
_NEXT_WEEK_RE = re.compile(r"\bnext\s+week\b")
_NEXT_MONTH_RE = re.compile(r"\bnext\s+month\b")
# "on the 1st", "the 15th", "on the 3rd of next month"
_DOM_RE = re.compile(r"\b(?:on\s+|by\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)\b(?:\s+of\s+(this|next)\s+month)?")

RECURRENCE_PATTERNS = [
    (re.compile(r"\b(every\s*day|daily|each\s+day|every\s+morning|every\s+evening|every\s+night)\b"), "daily"),
    (re.compile(r"\b(every\s+weekday|weekdays|on\s+weekdays)\b"), "weekdays"),
    (re.compile(r"\b(every\s+week|weekly|each\s+week|every\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b"), "weekly"),
    (re.compile(r"\b(every\s+month|monthly|each\s+month)\b"), "monthly"),
]


def get_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


@dataclass
class ParsedWhen:
    dt: datetime  # aware, in the user's timezone
    has_date: bool
    has_time: bool
    ambiguous_meridiem: bool = False
    relative: bool = False
    spans: list[tuple[int, int]] = field(default_factory=list)

    @property
    def utc(self) -> datetime:
        return self.dt.astimezone(timezone.utc)


def _num(token: str) -> float:
    token = token.strip()
    try:
        return float(token)
    except ValueError:
        return float(NUMBER_WORDS.get(token, 1))


def _delta(n: float, unit: str) -> timedelta:
    u = unit.rstrip("s")
    if u in ("minute", "min"):
        return timedelta(minutes=n)
    if u in ("hour", "hr"):
        return timedelta(hours=n)
    if u == "day":
        return timedelta(days=n)
    if u == "week":
        return timedelta(weeks=n)
    if u == "month":
        return timedelta(days=30 * n)
    raise ValueError(unit)


def _local_dt(d: date, t: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, t).replace(tzinfo=tz)


def parse_recurrence(text: str) -> str | None:
    low = text.lower()
    for pat, rec in RECURRENCE_PATTERNS:
        if pat.search(low):
            return rec
    return None


def _parse_time(low: str) -> tuple[time | None, bool, list[tuple[int, int]], str | None]:
    """Returns (time, ambiguous_meridiem, spans, part_of_day)."""
    spans: list[tuple[int, int]] = []
    pod = None
    m_pod = _POD_RE.search(low)
    if m_pod:
        pod = m_pod.group(1)

    m = _TIME_AMPM_RE.search(low)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if not (1 <= hour <= 12 and minute < 60):
            return None, False, spans, pod
        pm = m.group(3).startswith("p")
        hour = hour % 12 + (12 if pm else 0)
        start = m.start()
        pre = re.search(r"(?:at|@|by|around)\s+$", low[:start])
        spans.append((pre.start() if pre else start, m.end()))
        return time(hour, minute), False, spans, pod

    m = _NOON_RE.search(low)
    if m:
        spans.append(m.span())
        pre = re.search(r"(?:at|by)\s+$", low[: m.start()])
        if pre:
            spans.append(pre.span())
        return (time(0, 0) if m.group(1) == "midnight" else time(12, 0)), False, spans, pod

    m = _TIME_24_RE.search(low)
    if m and int(m.group(1)) >= 13 or (m and m.group(1).startswith("0")):
        pre = re.search(r"(?:at|@|by|around)\s+$", low[: m.start()])
        spans.append((pre.start() if pre else m.start(), m.end()))
        return time(int(m.group(1)), int(m.group(2))), False, spans, pod

    m = _TIME_AT_RE.search(low)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if hour > 23 or minute > 59:
            return None, False, spans, pod
        spans.append(m.span())
        if hour == 0 or hour >= 12:
            return time(hour, minute), False, spans, pod
        if pod in ("afternoon", "evening", "night", "tonight"):
            return time(hour + 12, minute), False, spans, pod
        if pod == "morning":
            return time(hour, minute), False, spans, pod
        # No meridiem: 1-6 almost always means PM in everyday speech; 7-11 means AM.
        guess = hour + 12 if hour <= 6 else hour
        return time(guess, minute), True, spans, pod

    return None, False, spans, pod


def _parse_date(low: str, today: date) -> tuple[date | None, list[tuple[int, int]], bool]:
    """Returns (date, spans, explicit_future_required)."""
    m = _DAY_AFTER_RE.search(low)
    if m:
        return today + timedelta(days=2), [m.span()], False
    m = _TOMORROW_RE.search(low)
    if m:
        return today + timedelta(days=1), [m.span()], False
    m = _ISO_DATE_RE.search(low)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), [m.span()], False
        except ValueError:
            pass
    for rx, order in ((_DM_RE, "dm"), (_MD_RE, "md")):
        m = rx.search(low)
        if m:
            if order == "dm":
                day, mon, year = int(m.group(1)), MONTHS[m.group(2)], m.group(3)
            else:
                mon, day, year = MONTHS[m.group(1)], int(m.group(2)), m.group(3)
            try:
                d = date(int(year) if year else today.year, mon, day)
            except ValueError:
                continue
            if not year and d < today:
                d = d.replace(year=today.year + 1)
            pre = re.search(r"(?:on|by)\s+$", low[: m.start()])
            return d, [(pre.start() if pre else m.start(), m.end())], False
    m = _DOM_RE.search(low)
    if m:
        day = int(m.group(1))
        y, mo = today.year, today.month
        if m.group(2) == "next" or (m.group(2) is None and day < today.day):
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        # Clamp e.g. "the 31st" to the month's last day (Feb → 28/29).
        if 1 <= day <= 31:
            d = date(y, mo, min(day, calendar.monthrange(y, mo)[1]))
            return d, [m.span()], m.group(2) is None and day == today.day
    m = _NEXT_WEEK_RE.search(low)
    if m:
        return today + timedelta(days=(7 - today.weekday()) or 7), [m.span()], False
    m = _NEXT_MONTH_RE.search(low)
    if m:
        y, mo = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return date(y, mo, 1), [m.span()], False
    m = _WEEKDAY_RE.search(low)
    if m:
        qualifier, wd = m.group(1), WEEKDAYS[m.group(2)]
        ahead = (wd - today.weekday()) % 7
        if qualifier == "next" and ahead == 0:
            ahead = 7
        return today + timedelta(days=ahead), [m.span()], ahead == 0
    m = _TODAY_RE.search(low)
    if m:
        return today, [m.span()], False
    return None, [], False


def parse_when(text: str, now: datetime, prefer_am: bool = False) -> ParsedWhen | None:
    """Resolve a natural-language time expression relative to `now` (aware, user tz).

    `prefer_am` resolves a bare hour ("wake me up at 6:30") to the morning instead of
    guessing, and clears the ambiguity flag.
    """
    if not text:
        return None
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    low = " " + text.lower().replace("’", "'") + " "
    tz = now.tzinfo

    # 1) Relative durations: "in 2 hours", "after three days", "half an hour later"
    m = _HALF_HOUR_RE.search(low)
    if m:
        return ParsedWhen(now + timedelta(minutes=30), True, True, relative=True, spans=[_shift(m.span())])
    m = _REL_RE.search(low) or _REL_FROM_NOW_RE.search(low)
    if m:
        delta = _delta(_num(m.group(1)), m.group(2))
        target = now + delta
        spans = [_shift(m.span())]
        unit = m.group(2).rstrip("s")
        if unit in ("day", "week", "month"):
            t, amb, tspans, pod = _parse_time(low)
            if t is None and pod:
                t = time(PART_OF_DAY[pod], 0)
            if t is not None:
                spans += [_shift(s) for s in tspans]
                return ParsedWhen(_local_dt(target.date(), t, tz), True, True, amb, True, spans)
            return ParsedWhen(target.replace(second=0, microsecond=0), True, True, relative=True, spans=spans)
        return ParsedWhen(target.replace(microsecond=0), True, True, relative=True, spans=spans)

    # 2) Absolute date and/or time
    today = now.date()
    d, dspans, _ = _parse_date(low, today)
    t, amb, tspans, pod = _parse_time(low)
    spans = [_shift(s) for s in dspans + tspans]

    is_tonight = bool(re.search(r"\btonight\b", low))
    if t is None and pod:
        t = time(PART_OF_DAY[pod], 0)
        m_pod = _POD_RE.search(low)
        if m_pod:
            spans.append(_shift(m_pod.span()))
    if t is not None and is_tonight and t.hour < 12 and amb:
        t = time(t.hour + 12, t.minute)
        amb = False
    if t is not None and amb and prefer_am:
        t = time(t.hour % 12, t.minute)
        amb = False

    if d is None and t is None:
        return None
    if d is None:
        if amb:
            # Bare hour with no date ("remind me at 8"): take whichever of AM/PM comes next.
            candidates = []
            for hour in {t.hour % 12, t.hour % 12 + 12}:
                c = _local_dt(today, time(hour, t.minute), tz)
                candidates.append(c if c > now else c + timedelta(days=1))
            return ParsedWhen(min(candidates), False, True, True, spans=spans)
        dt = _local_dt(today, t, tz)
        if dt <= now:
            dt += timedelta(days=1)
        return ParsedWhen(dt, False, True, amb, spans=spans)
    if t is None:
        return ParsedWhen(_local_dt(d, time(9, 0), tz), True, False, spans=spans)
    dt = _local_dt(d, t, tz)
    if dt <= now and _WEEKDAY_RE.search(low) and not _TODAY_RE.search(low):
        dt += timedelta(days=7)  # "Friday at 3pm" said on Friday at 5pm → next Friday
    return ParsedWhen(dt, True, True, amb, spans=spans)


def _shift(span: tuple[int, int]) -> tuple[int, int]:
    # parse_when pads the text with one leading space
    return max(span[0] - 1, 0), max(span[1] - 1, 0)


def strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    keep = []
    pos = 0
    for start, end in sorted(spans):
        if start > pos:
            keep.append(text[pos:start])
        pos = max(pos, end)
    keep.append(text[pos:])
    return re.sub(r"\s{2,}", " ", "".join(keep)).strip(" ,.")


def parse_period(text: str, now: datetime) -> tuple[datetime, datetime, str] | None:
    """Resolve a *past* period for searching ("yesterday", "last week", "3 days ago")."""
    low = text.lower()
    tz = now.tzinfo
    today = now.date()

    def day_range(d: date, days: int = 1) -> tuple[datetime, datetime]:
        start = _local_dt(d, time(0), tz)
        return start, start + timedelta(days=days)

    if re.search(r"\b(the\s+)?day\s+before\s+yesterday\b", low):
        return (*day_range(today - timedelta(days=2)), "the day before yesterday")
    if re.search(r"\byesterday\b", low):
        return (*day_range(today - timedelta(days=1)), "yesterday")
    if re.search(r"\b(today|this morning|earlier today)\b", low):
        return (*day_range(today), "today")
    m = re.search(rf"\b{_NUM}\s+days?\s+ago\b", low)
    if m:
        return (*day_range(today - timedelta(days=int(_num(m.group(1))))), m.group(0))
    m = re.search(rf"\b{_NUM}\s+weeks?\s+ago\b", low)
    if m:
        n = int(_num(m.group(1)))
        start = today - timedelta(days=today.weekday() + 7 * n)
        return (*day_range(start, 7), m.group(0))
    if re.search(r"\blast\s+week\b", low):
        start = today - timedelta(days=today.weekday() + 7)
        return (*day_range(start, 7), "last week")
    if re.search(r"\bthis\s+week\b", low):
        start = today - timedelta(days=today.weekday())
        return (*day_range(start, (today - start).days + 1), "this week")
    if re.search(r"\blast\s+month\b", low):
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        start = last_prev.replace(day=1)
        return (*day_range(start, (first_this - start).days), "last month")
    if re.search(r"\b(recently|lately|the other day)\b", low):
        return (*day_range(today - timedelta(days=6), 7), "the last 7 days")
    m = re.search(r"\b(?:on|last)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", low)
    if m:
        back = (today.weekday() - WEEKDAYS[m.group(1)]) % 7 or 7
        return (*day_range(today - timedelta(days=back)), f"last {m.group(1).title()}")
    return None


def next_occurrence(current: datetime, recurrence: str, tz_name: str, after: datetime) -> datetime:
    """Next fire time strictly after `after`, preserving local wall-clock time across DST."""
    tz = get_tz(tz_name)
    local = current.astimezone(tz)
    wall = local.timetz().replace(tzinfo=None)
    d = local.date()
    for _ in range(1000):
        if recurrence == "daily":
            d += timedelta(days=1)
        elif recurrence == "weekdays":
            d += timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
        elif recurrence == "weekly":
            d += timedelta(days=7)
        elif recurrence == "monthly":
            y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
            d = date(y, m, min(local.day, calendar.monthrange(y, m)[1]))
        else:
            raise ValueError(f"unknown recurrence {recurrence!r}")
        candidate = _local_dt(d, wall, tz)
        if candidate > after:
            return candidate.astimezone(timezone.utc)
    raise ValueError("could not compute next occurrence")


def humanize(dt: datetime, now: datetime) -> str:
    """'tomorrow at 8:00 PM', 'today at 3:00 PM', 'Mon, Nov 9 at 10:00 AM'."""
    local = dt.astimezone(now.tzinfo)
    t = local.strftime("%I:%M %p").lstrip("0")
    delta_days = (local.date() - now.date()).days
    if delta_days == 0:
        return f"today at {t}"
    if delta_days == 1:
        return f"tomorrow at {t}"
    if delta_days == -1:
        return f"yesterday at {t}"
    if 1 < delta_days < 7:
        return f"{local.strftime('%A')} at {t}"
    fmt = "%a, %b %d" if local.year == now.year else "%a, %b %d %Y"
    return f"{local.strftime(fmt).replace(' 0', ' ')} at {t}"


def humanize_date(d: datetime, now: datetime) -> str:
    local = d.astimezone(now.tzinfo)
    delta_days = (local.date() - now.date()).days
    if delta_days == 0:
        return "today"
    if delta_days == 1:
        return "tomorrow"
    if 1 < delta_days < 7:
        return local.strftime("%A")
    return local.strftime("%a, %b %d").replace(" 0", " ")
