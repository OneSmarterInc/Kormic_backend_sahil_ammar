from __future__ import annotations

from institutes.models import Institute


def register_institute(
    name: str,
    contact_email: str = "",
    contact_phone: str = "",
    address: str = "",
) -> Institute:
    """Create an Institute row (integer PK + auto uuid) -- the whole
    registration flow (no setup phase, unlike universities.register_university,
    since institutes carry no persona/agent configuration)."""
    return Institute.objects.create(
        name=name.strip(),
        contact_email=contact_email.strip(),
        contact_phone=contact_phone.strip(),
        address=address.strip(),
    )
