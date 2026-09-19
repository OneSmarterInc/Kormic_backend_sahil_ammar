"""Sanitized model-call telemetry. No prompts, replies, names, email or provider IDs."""
from contextvars import ContextVar
from datetime import timedelta
from decimal import Decimal, DecimalException
import os
import json
import time
import uuid
from django.db.models import Sum, Count
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from accounts.permissions import IsSuperUserRole, IsTOTPEnrolled

current_request = ContextVar("telemetry_request", default=None)


def usage_values(response):
    usage = getattr(response, "usage", None)
    if usage is None:
        generations = getattr(response, "generations", [])
        usage = getattr(generations[0].message, "usage_metadata", None) if generations else None
    if not usage: return None, None
    data = usage if isinstance(usage, dict) else usage.model_dump()
    if not isinstance(data, dict): return None, None
    return (int(data.get("input_tokens", 0)) + int(data.get("cache_creation_input_tokens", 0)) + int(data.get("cache_read_input_tokens", 0)), int(data.get("output_tokens", 0)))


def approximate_cost(input_tokens, output_tokens, model=""):
    # Per-model invoice rates are deployment configuration, not a hardcoded
    # claim about provider pricing. Unconfigured models are visibly unknown.
    try:
        rate = json.loads(os.getenv("MODEL_COST_RATES_JSON", "{}"))[model]
        return (Decimal(input_tokens or 0) * Decimal(str(rate["input"])) + Decimal(output_tokens or 0) * Decimal(str(rate["output"]))) / Decimal(1000000)
    except (KeyError, ValueError, TypeError, DecimalException):
        return None


def standalone_call(model, invoke):
    from django_api.models import ChatModelCall
    request = current_request.get()
    user = getattr(request, "user", None)
    account_id = getattr(getattr(user, "account", None), "pk", None) if getattr(user, "is_authenticated", False) else None
    row = ChatModelCall.objects.create(model=model, request_id=getattr(request, "request_id", None) or str(uuid.uuid4()),
        account_id=account_id, charged_tokens=0, estimated_cost_usd=0)
    started = time.monotonic()
    try:
        response = invoke()
        row.input_tokens, row.output_tokens = usage_values(response)
        row.status = "completed" if row.input_tokens is not None else "usage_unknown"
        row.actual_cost_usd = approximate_cost(row.input_tokens, row.output_tokens, model) if row.input_tokens is not None else None
        return response
    except BaseException as exc:
        row.status = "failed_or_unknown"
        row.error_category = "timeout" if "timeout" in type(exc).__name__.lower() else "provider_error"
        raise
    finally:
        row.latency_ms = int((time.monotonic() - started) * 1000)
        row.save()


def percentile(values, fraction):
    if not values: return None
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * fraction))]


class ModelTelemetryView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsSuperUserRole]
    def get(self, request):
        from django_api.models import ChatGeneration, ChatModelCall
        try: days = max(1, min(90, int(request.query_params.get("days", 7))))
        except ValueError: days = 7
        since = timezone.now() - timedelta(days=days)
        calls = ChatModelCall.objects.filter(created_at__gte=since)
        jobs = ChatGeneration.objects.filter(created_at__gte=since)
        latency = list(jobs.exclude(latency_ms__isnull=True).values_list("latency_ms", flat=True))
        totals = calls.aggregate(model_calls=Count("pk"), input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"), reserved_cost_usd=Sum("estimated_cost_usd"), approximate_cost_usd=Sum("actual_cost_usd"))
        return Response({"days": days, "totals": totals,
            "generation_count": jobs.count(), "tool_calls": jobs.aggregate(total=Sum("tool_calls"))["total"] or 0,
            "university_fanout": jobs.aggregate(total=Sum("university_fanout"))["total"] or 0,
            "latency_ms": {"p50": percentile(latency, .5), "p95": percentile(latency, .95), "p99": percentile(latency, .99)},
            "models": list(calls.values("model").annotate(calls=Count("pk"), input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"), approximate_cost_usd=Sum("actual_cost_usd"))),
            "errors": list(jobs.exclude(error_code="").values("error_code").annotate(count=Count("pk"))),
            "model_errors": list(calls.exclude(error_category="").values("model", "error_category").annotate(count=Count("pk"))),
            "unpriced_calls": calls.filter(actual_cost_usd__isnull=True).count(),
            "unknown_usage_calls": calls.exclude(status="completed").count(),
            "cost_note": "Estimates use configured rates, not provider invoices. Unknown usage is not zero cost."})
