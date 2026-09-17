
from __future__ import annotations

import random
from typing import Optional

from agents.agent_identity import DEFAULT_NAME_POOL


def _normalized(name: str) -> str:
    return str(name or "").strip()


def is_agent_name_available(name: str, exclude_university_id: Optional[str] = None) -> bool:
    """Case-insensitive uniqueness check across every university's agent name."""
    from universities.models import University

    name = _normalized(name)
    if not name:
        return False

    query = University.objects.filter(agent_name__iexact=name)
    if exclude_university_id:
        # exclude_university_id is the public uuid string, not the int PK.
        query = query.exclude(uuid=exclude_university_id)

    return not query.exists()


def generate_unique_agent_name() -> str:
    """Pick an available default name, falling back to a numbered variant
    once the base pool is exhausted (e.g. "Nova 2")."""
    from universities.models import University

    used = {
        (n or "").strip().lower()
        for n in University.objects.exclude(agent_name__isnull=True).values_list("agent_name", flat=True)
    }

    candidates = [n for n in DEFAULT_NAME_POOL if n.lower() not in used]
    if candidates:
        return random.choice(candidates)

    base = random.choice(DEFAULT_NAME_POOL)
    suffix = 2
    while f"{base} {suffix}".lower() in used:
        suffix += 1
    return f"{base} {suffix}"


def ensure_agent_name(university) -> str:
    """Assign a default agent name to a University row if it doesn't have
    one yet, and return the resolved name."""
    if university.agent_name:
        return university.agent_name

    university.agent_name = generate_unique_agent_name()
    university.save(update_fields=["agent_name", "updated_at"])
    return university.agent_name
