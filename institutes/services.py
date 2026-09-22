from __future__ import annotations

from institutes.country_codes import normalize_country_code
from institutes.models import Institute


def register_institute(
    name: str,
    country: str,
    contact_email: str = "",
    contact_phone: str = "",
    address: str = "",
) -> Institute:
    """Create an Institute row (integer PK + auto uuid) -- the whole
    registration flow (no setup phase, unlike universities.register_university,
    since institutes carry no persona/agent configuration)."""
    country_code = normalize_country_code(country)
    if country_code == "US":
        raise ValueError("Institutes cannot use country US.")

    return Institute.objects.create(
        name=name.strip(),
        country=country_code,
        contact_email=contact_email.strip(),
        contact_phone=contact_phone.strip(),
        address=address.strip(),
    )
