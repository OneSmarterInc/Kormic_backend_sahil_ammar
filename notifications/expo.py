
from __future__ import annotations

from typing import Any, Dict, List

import requests
from django.conf import settings

PUSH_URL = "https://exp.host/--/api/v2/push/send"
RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"

# Expo caps each push request at 100 messages.
MAX_BATCH_SIZE = 100


class ExpoPushError(Exception):
    """Raised when Expo's API itself reports an error for the whole request
    (as opposed to a per-message ticket error, which is returned, not raised)."""


def _headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    access_token = getattr(settings, "EXPO_PUSH_ACCESS_TOKEN", "")
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def send_expo_push_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Send up to MAX_BATCH_SIZE messages in one request. Each message is a dict
    with at least "to" (an ExponentPushToken[...] string); "title", "body",
    "data", "sound" are the common optional fields.

    Returns Expo's per-message ticket list, in the same order as `messages`.
    Each ticket is either {"status": "ok", "id": "<receipt-id>"} or
    {"status": "error", "message": ..., "details": {...}} -- a ticket error
    does not raise, since one bad token in a batch shouldn't fail the batch.
    """
    if not messages:
        return []
    if len(messages) > MAX_BATCH_SIZE:
        raise ValueError(f"send_expo_push_messages: {len(messages)} messages exceeds Expo's batch limit of {MAX_BATCH_SIZE}")

    response = requests.post(PUSH_URL, json=messages, headers=_headers(), timeout=10)
    response.raise_for_status()
    payload = response.json()

    if payload.get("errors"):
        raise ExpoPushError(str(payload["errors"]))

    return payload.get("data", [])


def get_expo_push_receipts(receipt_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Look up delivery receipts for previously-sent ticket ids. Returns a
    dict keyed by receipt id, e.g. {"<id>": {"status": "error", "details": {"error": "DeviceNotRegistered"}}}."""
    if not receipt_ids:
        return {}

    response = requests.post(RECEIPTS_URL, json={"ids": receipt_ids}, headers=_headers(), timeout=10)
    response.raise_for_status()
    payload = response.json()

    if payload.get("errors"):
        raise ExpoPushError(str(payload["errors"]))

    return payload.get("data", {})
