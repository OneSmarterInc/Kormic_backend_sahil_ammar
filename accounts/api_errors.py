"""Canonical API wire errors, including middleware and unknown-route failures."""
import re
import uuid
from django.http import JsonResponse
from rest_framework.renderers import JSONRenderer

CODES = {400: "VALIDATION_ERROR", 401: "AUTHENTICATION_REQUIRED", 403: "PERMISSION_DENIED",
         404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 409: "CONFLICT", 413: "PAYLOAD_TOO_LARGE",
         415: "UNSUPPORTED_MEDIA_TYPE", 429: "RATE_LIMITED", 500: "INTERNAL_ERROR", 503: "SERVICE_UNAVAILABLE"}
MESSAGES = {400: "Please check the submitted information.", 401: "Please sign in again.",
            403: "You do not have permission for this action.", 404: "Resource not found.",
            429: "Too many requests. Please try again later.", 500: "Something went wrong. Please try again later.",
            503: "Service temporarily unavailable. Please try again later."}

def envelope(data, status, request, code=None):
    data = {"non_field_errors": data} if isinstance(data, list) else data if isinstance(data, dict) else {}
    nested = data.get("error") if isinstance(data.get("error"), dict) else {}
    proposed = code or nested.get("code") or data.get("code")
    error_code = proposed if isinstance(proposed, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", proposed) else CODES.get(status, "REQUEST_FAILED")
    if status == 404 and getattr(request, "path", "").startswith(("/api/profile/", "/api/v1/profile/")):
        error_code = "PROFILE_NOT_FOUND"
    message = nested.get("message") or data.get("message") or data.get("detail")
    if status < 500 and not message and isinstance(data.get("error"), str):
        message = data["error"]
    if status == 400 and not message and isinstance(data.get("non_field_errors"), list):
        message = next((item for item in data["non_field_errors"] if isinstance(item, str)), None)
    if status >= 500 or not isinstance(message, str):
        message = MESSAGES.get(status, "The request could not be completed.")
    details = {}
    if status == 400:
        details = {key: value for key, value in data.items() if key not in ("error", "message", "detail", "status", "code") and isinstance(value, (list, dict))}
        if isinstance(nested.get("details"), dict):
            details = nested["details"]
    return {"error": {"code": error_code, "message": message, "details": details,
                      "request_id": getattr(request, "request_id", str(uuid.uuid4()))}}

class APIJSONRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        context = renderer_context or {}
        response = context.get("response")
        if response is not None and response.status_code >= 400:
            data = envelope(data, response.status_code, context.get("request"), getattr(response, "error_code", None))
        return super().render(data, accepted_media_type, renderer_context)

class APIRequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = str(uuid.uuid4())
        from django_api.telemetry import current_request
        token = current_request.set(request)
        try:
            response = self.get_response(request)
            if getattr(request.user, "is_authenticated", False):
                from accounts.models import Account
                from django.utils import timezone
                from django.core.cache import cache
                if cache.add(f"account-activity:{request.user.pk}", True, 300):
                    Account.objects.filter(user=request.user).update(last_active_at=timezone.now())
        finally:
            current_request.reset(token)
        if request.path.startswith("/api/"):
            response["X-Request-ID"] = request.request_id
            versioned = request.path.startswith("/api/v1/")
            response["X-API-Version"] = "v1" if versioned else "legacy"
            if not versioned and not re.match(r"^/api/v[0-9]+/", request.path):
                successor = "/api/v1/" + request.get_full_path()[5:]
                response["Link"] = f'<{successor}>; rel="successor-version"' 
            # DRF already used APIJSONRenderer. Normalize Django errors (404,
            # CSRF middleware, request-size limits) without exposing HTML/debug pages.
            if response.status_code >= 400 and not hasattr(response, "accepted_renderer"):
                replacement = JsonResponse(envelope({}, response.status_code, request), status=response.status_code)
                for name, value in response.items():
                    if name.lower() not in ("content-type", "content-length"):
                        replacement[name] = value
                replacement.cookies = response.cookies
                response = replacement
        return response
