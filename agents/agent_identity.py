# agents/agent_identity.py
# Naming for the student's personal agent -- the single entry point each
# student talks to. Every student gets an auto-generated default name on
# first use and can rename it afterward, subject to uniqueness.

from __future__ import annotations

import random
from typing import Optional

DEFAULT_NAME_POOL = [
    "Aria", "Nova", "Atlas", "Sage", "Juno", "Orion", "Iris", "Kai",
    "Lumen", "Ember", "Vega", "Milo", "Zara", "Finn", "Skye", "Rune",
    "Indie", "Cove", "Wren", "Arlo", "Nyra", "Talon", "Elio", "Vesper",
    "Rowan", "Sol", "Pax", "Halo", "Cypher", "Onyx",
]


def _normalized(name: str) -> str:
    return str(name or "").strip()


def is_agent_name_available(name: str, exclude_student_id: Optional[str] = None) -> bool:
    """Case-insensitive uniqueness check across every student's agent name."""
    from django_api.models import StudentProfile

    name = _normalized(name)
    if not name:
        return False

    query = StudentProfile.objects.filter(agent_name__iexact=name)
    if exclude_student_id:
        # exclude_student_id is the public uuid string, not the int PK.
        query = query.exclude(uuid=exclude_student_id)

    return not query.exists()


def generate_unique_agent_name() -> str:
    """Pick an available default name, falling back to a numbered variant
    once the base pool is exhausted (e.g. "Nova 2")."""
    from django_api.models import StudentProfile

    used = {
        (n or "").strip().lower()
        for n in StudentProfile.objects.exclude(agent_name__isnull=True).values_list("agent_name", flat=True)
    }

    candidates = [n for n in DEFAULT_NAME_POOL if n.lower() not in used]
    if candidates:
        return random.choice(candidates)

    base = random.choice(DEFAULT_NAME_POOL)
    suffix = 2
    while f"{base} {suffix}".lower() in used:
        suffix += 1
    return f"{base} {suffix}"


def ensure_agent_name(profile) -> str:
    """Assign a default agent name to a StudentProfile row if it doesn't
    have one yet, and return the resolved name."""
    if not profile.agent_name:
        profile.agent_name = generate_unique_agent_name()
        profile.save(update_fields=["agent_name", "updated_at"])

    from agents.identity_registry import student_identity

    student_identity(str(profile.uuid), profile.agent_name)

    return profile.agent_name
