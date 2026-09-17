from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from url_discovery.rules_data import RULES_DIR


@lru_cache(maxsize=1)
def ignored_parameters() -> set[str]:
    path = RULES_DIR / "ignored_parameters.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(item).lower() for item in data.get("ignored_query_parameters", [])}


def normalize_url(url: str) -> str:
    """Return a stable URL used for duplicate detection and storage."""
    value = (url or "").strip()
    if not value:
        return ""

    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower().rstrip(".")
    if not scheme or not hostname:
        return ""

    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")

    ignored = ignored_parameters()
    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in ignored:
            continue
        query_pairs.append((key, value))
    query_pairs.sort(key=lambda pair: (pair[0].lower(), pair[1]))
    query = urlencode(query_pairs, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def extension_from_url(url: str) -> str:
    suffix = Path(urlsplit(url).path).suffix.lower()
    return suffix[:32]
