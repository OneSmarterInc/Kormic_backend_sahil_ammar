from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_AGENT_TIMEZONE = os.environ.get("AGENT_DEFAULT_TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"
PROFILE_TIMEZONE_KEYS = ("timezone", "time_zone")


def _resolve_timezone(
    student_profile: Optional[Dict[str, Any]] = None,
    timezone_name: Optional[str] = None,
) -> tuple[str, ZoneInfo]:
    candidate = (timezone_name or "").strip()

    if not candidate and isinstance(student_profile, dict):
        for key in PROFILE_TIMEZONE_KEYS:
            value = str(student_profile.get(key) or "").strip()
            if value:
                candidate = value
                break

    candidate = candidate or DEFAULT_AGENT_TIMEZONE

    try:
        return candidate, ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC", ZoneInfo("UTC")


def current_time_payload(
    student_profile: Optional[Dict[str, Any]] = None,
    *,
    timezone_name: Optional[str] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, str]:
    """Return authoritative current date/time context for the student agent.

    The student's saved IANA timezone wins when present. Otherwise the
    configurable AGENT_DEFAULT_TIMEZONE is used (Asia/Kolkata by default for
    Kormic's current student population). Invalid timezone values fail closed
    to UTC rather than letting the model guess.
    """
    tz_name, tz = _resolve_timezone(student_profile, timezone_name)

    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    local_now = now.astimezone(tz)

    return {
        "timezone": tz_name,
        "date": local_now.strftime("%A, %B %-d, %Y") if os.name != "nt" else local_now.strftime("%A, %B %#d, %Y"),
        "time": local_now.strftime("%H:%M:%S"),
        "iso": local_now.isoformat(),
        "utc_iso": now.isoformat(),
    }


def render_runtime_time_context(
    student_profile: Optional[Dict[str, Any]] = None,
    *,
    now_utc: Optional[datetime] = None,
) -> str:
    payload = current_time_payload(student_profile, now_utc=now_utc)
    return f"""

CURRENT DATE/TIME — AUTHORITATIVE RUNTIME CONTEXT:
Timezone: {payload['timezone']}
Current date: {payload['date']}
Current local time: {payload['time']}
Current timestamp: {payload['iso']}

Use this runtime clock as the source of truth for words such as "today",
"tomorrow", "yesterday", "this week", "this month", "current year", and for
reasoning about whether a deadline is past or upcoming. Never infer the current
date from model training knowledge or from old conversation messages. When a
question depends on a different timezone, use the get_current_datetime tool.
"""
