"""
Proactive agent outreach: the student agent noticing something worth
mentioning (a resume gap, a low profile score, a missing test score) and
messaging the student about it on its own, instead of waiting to be asked.

Deliberately template-based rather than another LLM call per student per
scan: django_api.services.compute_profile_intelligence() already computes
weaknesses/recommendations/completeness from live profile data (see its own
docstring -- those are NOT stale DB columns, they're recomputed fresh every
call), and agents.resume_parser already stores real gaps on intake. This
reuses that existing, already-trustworthy analysis instead of re-deriving it.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from django.utils import timezone

from notifications.models import NotificationLog

# resume_parser.py always appends these two regardless of what's actually
# missing (see agents/resume_parser.py:356-358) -- they're intake-flow
# placeholders, not real gaps worth proactively messaging about.
_ALWAYS_PRESENT_PLACEHOLDER_GAPS = {"budget", "target_disciplines"}

DEFAULT_COOLDOWN_DAYS = 7
MAX_TALKING_POINTS = 3


def _recently_nudged(account, cooldown_days: int) -> bool:
    cutoff = timezone.now() - timedelta(days=cooldown_days)
    return NotificationLog.objects.filter(
        account=account,
        event_type=NotificationLog.EventType.PROACTIVE_CHECKIN,
        created_at__gte=cutoff,
    ).exists()


def build_checkin_message(profile: Dict[str, Any]) -> Optional[str]:
    """
    Return a friendly one-line proactive nudge grounded in this student's
    already-computed profile gaps/weaknesses/missing fields, or None if
    there's genuinely nothing worth interrupting them for.
    """
    from django_api.services import compute_profile_intelligence

    real_gaps: List[str] = [
        gap for gap in (profile.get("gaps") or []) if gap not in _ALWAYS_PRESENT_PLACEHOLDER_GAPS
    ]

    intelligence = compute_profile_intelligence(profile)
    weaknesses: List[str] = intelligence["weaknesses"]
    missing_items: List[str] = intelligence["profile_completeness"]["missing_items"]

    # Prefer the most concrete signal available: an explicit gap the resume
    # parser flagged, then a scored weakness, then a bare "field is empty".
    talking_points = real_gaps or weaknesses or missing_items
    if not talking_points:
        return None

    talking_points = talking_points[:MAX_TALKING_POINTS]

    if len(talking_points) == 1:
        return f"Quick suggestion -- {talking_points[0]}. Want to sort it out together?"

    joined = "; ".join(talking_points)
    return f"Quick suggestion -- I found a few gaps in your profile: {joined}. Want to fix these up?"


def run_checkin_for_student(
    student_id: str,
    *,
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    queue_push: bool = True,
) -> Optional[NotificationLog]:
    """
    Build and send one proactive nudge for this student, unless they were
    already nudged within the cooldown window or there's nothing to say.
    Safe to call repeatedly (e.g. once per scheduled scan) -- the cooldown
    check makes it idempotent within the window.

    queue_push=False leaves the push dispatch to the caller -- used when
    fanning out a scan across many students so their pushes can be sent as
    one batched Expo call (see notifications.tasks).
    """
    from accounts.models import Account
    from django_api.services import as_uuid, load_profile_data
    from notifications.services import send_agent_message

    if not as_uuid(student_id):
        return None
    account = Account.objects.filter(student_profile__uuid=student_id).first()
    if account is None:
        return None

    if _recently_nudged(account, cooldown_days):
        return None

    profile = load_profile_data(student_id)
    message = build_checkin_message(profile)
    if message is None:
        return None

    return send_agent_message(
        student_id=student_id,
        content=message,
        event_type=NotificationLog.EventType.PROACTIVE_CHECKIN,
        title="Quick suggestion from your agent",
        meta={"type": "proactive_checkin"},
        notification_data={"type": "proactive_checkin"},
        queue_push=queue_push,
    )
