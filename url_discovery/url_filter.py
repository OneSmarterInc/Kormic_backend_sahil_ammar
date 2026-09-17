from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

import yaml

from url_discovery.rules_data import RULES_DIR
from url_discovery.url_normalizer import extension_from_url


@lru_cache(maxsize=1)
def filter_rules() -> dict:
    path = RULES_DIR / "blocked_paths.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def hard_filter_url(url: str, include_documents: bool = True) -> tuple[bool, str | None]:
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        return False, "unsupported protocol"

    lowered_path = (parts.path or "/").lower()
    rules = filter_rules()

    for fragment in rules.get("blocked_path_fragments", []):
        if str(fragment).lower() in lowered_path:
            return False, f"blocked path: {fragment}"

    extension = extension_from_url(url)
    blocked_extensions = {str(item).lower() for item in rules.get("blocked_extensions", [])}
    document_extensions = {
        str(item).lower() for item in rules.get("supported_document_extensions", [])
    }

    if extension in blocked_extensions:
        return False, f"blocked file type: {extension}"
    if extension in document_extensions and not include_documents:
        return False, "document crawling disabled"

    query = parts.query.lower()
    if "replytocom=" in query or "ical=" in query or "download=calendar" in query:
        return False, "calendar/comment loop parameter"

    return True, None


def supported_document(url: str) -> bool:
    extension = extension_from_url(url)
    docs = {str(item).lower() for item in filter_rules().get("supported_document_extensions", [])}
    return extension in docs
