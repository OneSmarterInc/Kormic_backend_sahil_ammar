from __future__ import annotations

import os
import secrets
from datetime import timedelta
from typing import Any, Dict, Optional

import requests
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone

from accounts.crypto import decrypt_secret, encrypt_secret
from accounts.models import Account, GitHubOAuthConnection

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
REVOKE_URL_TEMPLATE = "https://api.github.com/applications/{client_id}/token"
USER_URL = "https://api.github.com/user"

# Least privilege: no scope requested at all. An authenticated request still
# gets the full 5,000/hour rate limit and can read GET /user + any public
# repo data -- the same data this app already reads unauthenticated today.
# We deliberately do NOT request "repo" scope since nothing here needs
# private-repo access.
OAUTH_SCOPE = ""

_STATE_TTL = 600  # 10 minutes -- long enough for the GitHub consent screen, short enough to bound CSRF exposure.
_STATE_KEY_PREFIX = "github_oauth:state:"


class GitHubOAuthError(RuntimeError):
    pass


class GitHubNotConnectedError(GitHubOAuthError):
    pass


def _client_id() -> str:
    value = os.getenv("GITHUB_OAUTH_CLIENT_ID")
    if not value:
        raise GitHubOAuthError("GITHUB_OAUTH_CLIENT_ID is not configured.")
    return value


def _client_secret() -> str:
    value = os.getenv("GITHUB_OAUTH_CLIENT_SECRET")
    if not value:
        raise GitHubOAuthError("GITHUB_OAUTH_CLIENT_SECRET is not configured.")
    return value


def _redirect_uri() -> str:
    value = os.getenv("GITHUB_OAUTH_REDIRECT_URI")
    if not value:
        raise GitHubOAuthError("GITHUB_OAUTH_REDIRECT_URI is not configured.")
    return value


# CSRF state (ties the callback, which carries no auth header, back to the
# student who started the flow -- same cache-backed one-time-token pattern
# accounts/mfa.py already uses for mfa_token).

def _state_key(state: str) -> str:
    return f"{_STATE_KEY_PREFIX}{state}"


def create_oauth_state(user_id: int) -> str:
    state = secrets.token_urlsafe(32)
    cache.set(_state_key(state), user_id, timeout=_STATE_TTL)
    return state


def consume_oauth_state(state: str) -> Optional[int]:
    """One-time read: returns the user_id that started the flow, or None if
    the state is missing/expired/already used, and invalidates it either way."""
    key = _state_key(state)
    user_id = cache.get(key)
    cache.delete(key)
    return user_id


def build_authorize_url(state: str) -> str:
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "state": state,
        "scope": OAUTH_SCOPE,
        "allow_signup": "false",
    }
    query = "&".join(f"{key}={requests.utils.quote(str(value), safe='')}" for key, value in params.items())
    return f"{AUTHORIZE_URL}?{query}"

# Token exchange / refresh / revoke

def exchange_code_for_token(code: str) -> Dict[str, Any]:
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "code": code,
            "redirect_uri": _redirect_uri(),
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("error"):
        raise GitHubOAuthError(payload.get("error_description") or payload["error"])

    if not payload.get("access_token"):
        raise GitHubOAuthError("GitHub did not return an access token.")

    return payload


def _refresh_with_token(refresh_token: str) -> Dict[str, Any]:
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("error") or not payload.get("access_token"):
        raise GitHubOAuthError(payload.get("error_description") or "GitHub token refresh failed.")

    return payload


def fetch_github_identity(access_token: str) -> Dict[str, Any]:
    response = requests.get(
        USER_URL,
        headers={
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {access_token}",
            "User-Agent": "Kormic-GitHub-OAuth",
        },
        timeout=15,
    )
    if response.status_code != 200:
        raise GitHubOAuthError(f"Could not verify GitHub identity (status {response.status_code}).")
    return response.json()


def _token_response_to_fields(token_response: Dict[str, Any]) -> Dict[str, Any]:
    expires_in = token_response.get("expires_in")
    return {
        "access_token_encrypted": encrypt_secret(token_response["access_token"]),
        "refresh_token_encrypted": (
            encrypt_secret(token_response["refresh_token"]) if token_response.get("refresh_token") else ""
        ),
        "token_expires_at": (
            timezone.now() + timedelta(seconds=int(expires_in)) if expires_in else None
        ),
        "scope": token_response.get("scope", "") or "",
    }


def save_connection(user: User, token_response: Dict[str, Any], identity: Dict[str, Any]) -> GitHubOAuthConnection:
    fields = _token_response_to_fields(token_response)
    connection, _ = GitHubOAuthConnection.objects.update_or_create(
        user=user,
        defaults={
            "github_user_id": identity["id"],
            "github_username": identity.get("login", ""),
            "github_name": identity.get("name") or "",
            "github_email": identity.get("email") or "",
            **fields,
        },
    )
    return connection


def get_connection_for_user(user: User) -> Optional[GitHubOAuthConnection]:
    return GitHubOAuthConnection.objects.filter(user=user).first()


def get_valid_access_token(connection: GitHubOAuthConnection) -> str:
    """Returns a usable access token, transparently refreshing it if GitHub
    issued an expiring token and it's due to expire."""
    if connection.token_expires_at and connection.token_expires_at <= timezone.now() + timedelta(minutes=1):
        if not connection.refresh_token_encrypted:
            raise GitHubOAuthError("GitHub connection expired and has no refresh token; reconnect required.")

        refresh_token = decrypt_secret(connection.refresh_token_encrypted)
        token_response = _refresh_with_token(refresh_token)
        fields = _token_response_to_fields(token_response)
        for field, value in fields.items():
            setattr(connection, field, value)
        connection.save(update_fields=list(fields.keys()) + ["updated_at"])

    return decrypt_secret(connection.access_token_encrypted)


def get_connection_for_student_id(student_id: str) -> Optional[GitHubOAuthConnection]:
    from django_api.services import as_uuid

    if not as_uuid(student_id):
        return None
    account = Account.objects.filter(student_profile__uuid=student_id).select_related("user").first()
    if account is None:
        return None

    return get_connection_for_user(account.user)


def revoke_and_delete(connection: GitHubOAuthConnection) -> None:
    """Best-effort remote revocation, then always drop the local row --
    a failed revoke call shouldn't block the student from disconnecting."""
    try:
        access_token = decrypt_secret(connection.access_token_encrypted)
        requests.delete(
            REVOKE_URL_TEMPLATE.format(client_id=_client_id()),
            auth=(_client_id(), _client_secret()),
            json={"access_token": access_token},
            headers={"Accept": "application/vnd.github+json"},
            timeout=15,
        )
    except Exception:
        pass

    connection.delete()
