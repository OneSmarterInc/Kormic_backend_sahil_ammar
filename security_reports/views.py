import json
from urllib.parse import urlsplit

from django.core.cache import cache
from django.db.models import F
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import CSPReport

# Exact production aliases verified in Vercel and via HTTP on 2026-10-26.
# Never allow *.vercel.app: it includes deployments owned by other tenants.
ALLOWED_DOCUMENT_HOSTS = frozenset({
    "platform.kormic.ai",
    "kormicfrontendammarsahil-kpkx.vercel.app",
    "kormic-frontend-institutes.vercel.app",
    "kormic-frontend-university.vercel.app",
    "kormic-frontend-superuser.vercel.app",
})

MAX_BYTES = 16384
DIRECTIVES = frozenset((
    "default-src", "script-src", "script-src-elem", "script-src-attr",
    "style-src", "style-src-elem", "style-src-attr", "font-src", "img-src",
    "connect-src", "object-src", "base-uri", "frame-ancestors", "frame-src",
    "worker-src", "media-src", "manifest-src", "form-action",
))


def blocked_origin(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    if value in {"inline", "eval", "data", "blob", "self"}:
        return value
    try:
        url = urlsplit(value)
        if url.scheme in {"data", "blob"}:
            return url.scheme
        if url.scheme in {"http", "https"} and url.hostname:
            # No credentials, path, query, fragment, or arbitrary URL contents.
            host = url.hostname.encode("idna").decode("ascii")
            if len(host) <= 240 and all(c.isalnum() or c in ".-" for c in host):
                return f"{url.scheme}://{host}"
    except (ValueError, UnicodeError):
        pass
    return "unknown"


@csrf_exempt
@require_POST
def collect(request):
    """Public browser telemetry, not an authenticated or authoritative event."""
    if request.content_type not in {"application/csp-report", "application/reports+json"}:
        return HttpResponse(status=415)
    try:
        if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BYTES:
            return HttpResponse(status=413)
    except ValueError:
        return HttpResponse(status=400)
    # Global bound also works behind a proxy without trusting forwarded IPs.
    key = f"csp-report-budget:{int(timezone.now().timestamp()) // 60}"
    try:
        if not cache.add(key, 1, timeout=120) and cache.incr(key) > 120:
            return HttpResponse(status=429, headers={"Retry-After": "60"})
    except Exception:
        return HttpResponse(status=503)
    raw = request.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        return HttpResponse(status=413)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return HttpResponse(status=400)
    if request.content_type == "application/csp-report":
        reports = [payload.get("csp-report")] if isinstance(payload, dict) else []
    else:
        if not isinstance(payload, list) or len(payload) > 20:
            return HttpResponse(status=400)
        reports = [item.get("body") for item in payload
                   if isinstance(item, dict) and item.get("type") == "csp-violation"]
    day = timezone.now().date()
    for report in reports:
        if not isinstance(report, dict):
            continue
        document = report.get("document-uri", report.get("documentURL"))
        try:
            url = urlsplit(document) if isinstance(document, str) else None
            if not url or url.scheme != "https" or url.netloc not in ALLOWED_DOCUMENT_HOSTS:
                continue
        except ValueError:
            continue
        directive = report.get("effective-directive", report.get("effectiveDirective"))
        if not isinstance(directive, str) or directive not in DIRECTIVES:
            continue
        origin = blocked_origin(report.get("blocked-uri", report.get("blockedURL")))
        rows = CSPReport.objects.filter(day=day, directive=directive, blocked_origin=origin)
        if rows.update(count=F("count") + 1):
            continue
        # Finite per-day origin cardinality even for spoofed browser reports.
        if CSPReport.objects.filter(day=day).count() >= 1000:
            continue
        _, created = CSPReport.objects.get_or_create(
            day=day, directive=directive, blocked_origin=origin
        )
        if not created:
            rows.update(count=F("count") + 1)
    return HttpResponse(status=204, headers={"Cache-Control": "no-store"})
