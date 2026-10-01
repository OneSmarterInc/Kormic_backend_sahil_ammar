"""Invitation link construction shared by the API and mail worker."""
from email.utils import formataddr, parseaddr
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def claim_link(token, base_url=None):
    value = (settings.CLAIM_PAGE_URL if base_url is None else base_url).strip()
    try:
        parts = urlsplit(value)
        valid = (parts.scheme in ("https", "http") and parts.hostname
                 and not parts.username and not parts.password
                 and parts.path in ("/claim", "/claim/") and not parts.fragment
                 and not any(ord(c) < 33 for c in value))
        if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "10.0.2.2"):
            valid = False
        parts.port  # Validate malformed ports before queueing any email.
        if not valid:
            raise ValueError()
    except ValueError:
        raise ImproperlyConfigured("CLAIM_PAGE_URL must be the public HTTPS student portal /claim URL (HTTP localhost is supported only for local testing).") from None
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True) if key != "token"]
    query.append(("token", token))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def sender():
    return formataddr(("Kormic", parseaddr(settings.DEFAULT_FROM_EMAIL)[1]))


def validate_delivery_url():
    # EMAIL_MODE selects SMTP credentials, not the frontend deployment target.
    # Use the configured URL for local testing as well as public deployments.
    claim_link("")
