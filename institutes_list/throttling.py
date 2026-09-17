# institutes_list/throttling.py
# Rate limiting for the anonymous claim endpoints (claim/start and
# claim/verify -- public by design because possession of the OTP is the
# authentication boundary). Both endpoints have independent per-IP and
# per-invitation budgets.
from __future__ import annotations

from django.conf import settings
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle


class ClaimRateSettingsMixin:
    """Read the configured claim rate when each throttle is instantiated.

    DRF caches its global ``api_settings`` object. Django's ``override_settings``
    can therefore leave a test using the previously-cached production rate,
    which makes the rate-limit regression suite environment-dependent. Claim
    throttles are security controls, so resolve their rates directly from the
    active Django settings instead of depending on that cache.
    """

    def get_rate(self):
        configured_rates = getattr(settings, "REST_FRAMEWORK", {}).get(
            "DEFAULT_THROTTLE_RATES", {}
        )
        configured_rate = configured_rates.get(self.scope)
        return configured_rate or super().get_rate()


class ClaimEmailRateThrottle(ClaimRateSettingsMixin, SimpleRateThrottle):
    def get_cache_key(self, request, view):
        ident = str(
            request.data.get("email") or request.data.get("token") or ""
        ).strip().lower()
        if not ident:
            # Nothing to key on -- the per-IP throttle still applies.
            return None
        return self.cache_format % {"scope": self.scope, "ident": ident}


class ClaimStartIPThrottle(ClaimRateSettingsMixin, AnonRateThrottle):
    scope = "claim_start_ip"


class ClaimStartEmailThrottle(ClaimEmailRateThrottle):
    scope = "claim_start_email"


class ClaimVerifyIPThrottle(ClaimRateSettingsMixin, AnonRateThrottle):
    scope = "claim_verify_ip"


class ClaimVerifyEmailThrottle(ClaimEmailRateThrottle):
    scope = "claim_verify_email"
