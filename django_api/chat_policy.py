"""Shared admission and model budgets. Fail closed; never use process-local quotas."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from decimal import Decimal
import json
import os
import threading
import time
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django_api.models import ChatGate, ChatLease, ChatModelCall, StudentProfile, ChatGeneration


def limit(name, default):
    return max(1, int(os.environ.get(name, default)))


CALL_SECONDS = min(limit("CHAT_MODEL_TIMEOUT_SECONDS", 20), 30)
TURN_SECONDS = min(limit("CHAT_TURN_SECONDS", 75), 80)
HARD_SECONDS = 90
QUEUE_SECONDS = 60
LEASE_SECONDS = QUEUE_SECONDS + HARD_SECONDS + 30
MAX_MESSAGE_CHARS = limit("CHAT_MAX_MESSAGE_CHARS", 8000)
MAX_ATTACHMENT_BYTES = limit("CHAT_MAX_ATTACHMENT_BYTES", 5 * 1024 * 1024)
MAX_TOTAL_ATTACHMENT_BYTES = limit("CHAT_MAX_TOTAL_ATTACHMENT_BYTES", 10 * 1024 * 1024)
MAX_ATTACHMENTS = min(limit("CHAT_MAX_ATTACHMENTS", 3), 3)


class ChatPolicyError(Exception):
    def __init__(self, code, message, http_status=429):
        self.code, self.message, self.http_status = code, message, http_status
        super().__init__(message)


class TurnStopped(BaseException):
    # Tool implementations catch Exception and return a fallback. A budget stop
    # must unwind the WHOLE graph, not become another prompt/model iteration.
    def __init__(self, code="CHAT_TIMEOUT", message="Your agent took too long. Please try a shorter question."):
        self.code, self.message = code, message
        super().__init__(message)


current_budget = ContextVar("chat_turn_budget", default=None)


@contextmanager
def gate(key):
    ChatGate.objects.get_or_create(key=key)
    with transaction.atomic():
        ChatGate.objects.select_for_update().get(key=key)
        yield


@contextmanager
def university_slot(university_id):
    budget = current_budget.get()
    if budget is None:
        yield
        return
    budget.check()
    key = "university:" + str(university_id)
    with gate(key):
        ChatLease.objects.filter(gate_id=key, expires_at__lte=timezone.now()).delete()
        if ChatLease.objects.filter(gate_id=key).count() >= limit("CHAT_UNIVERSITY_CONCURRENCY", 2):
            raise TurnStopped("CHAT_UNIVERSITY_BUSY", "This university is busy. Please try again shortly.")
        lease = ChatLease.objects.create(gate_id=key, generation_id=budget.job_id,
                                        expires_at=timezone.now() + timedelta(seconds=HARD_SECONDS + 30))
    try:
        yield
    finally:
        lease.delete()


class TurnBudget:
    def __init__(self, job):
        self.job_id, self.student_id = job.pk, job.student_id
        self.account_id, self.request_id = job.account_id, job.request_id
        self.deadline = time.monotonic() + TURN_SECONDS
        self.lock = threading.Lock()
        self.tool_count = 0
        self.call_count = 0
        self.university_count = 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise TurnStopped()

    def tool(self):
        self.check()
        with self.lock:
            self.tool_count += 1
            ChatGeneration.objects.filter(pk=self.job_id).update(tool_calls=self.tool_count)
            if self.tool_count > limit("CHAT_MAX_TOOL_CALLS", 8):
                raise TurnStopped("CHAT_TOOL_LIMIT", "This request needs too many steps. Please narrow your question.")

    def universities(self, ids):
        ids = list(dict.fromkeys(ids))
        with self.lock:
            self.university_count += len(ids)
            ChatGeneration.objects.filter(pk=self.job_id).update(university_fanout=self.university_count)
            if self.university_count > limit("CHAT_MAX_UNIVERSITIES_PER_TURN", 4):
                raise TurnStopped("CHAT_FANOUT_LIMIT", "Please compare at most four universities in one question.")
        return ids

    def reserve(self, model, payload, max_tokens, counted_input=None):
        self.check()
        with self.lock:
            self.call_count += 1
            if self.call_count > limit("CHAT_MAX_MODEL_CALLS", 10):
                raise TurnStopped("CHAT_MODEL_LIMIT", "Please narrow your question and try again.")
        # UTF-8 byte count deliberately overestimates text tokens; base64 is
        # retained, making image/document reservations conservative as well.
        # Include serialized tool schemas and a protocol margin.
        input_bound = (counted_input if counted_input is not None else len(json.dumps(payload, ensure_ascii=False, default=str).encode())) + 4096
        token_bound = input_bound + max_tokens
        input_rate = Decimal(os.environ.get("CHAT_INPUT_USD_PER_MILLION", "15"))
        output_rate = Decimal(os.environ.get("CHAT_OUTPUT_USD_PER_MILLION", "75"))
        if input_rate <= 0 or output_rate <= 0:
            raise TurnStopped("CHAT_POLICY_CONFIG", "Chat is temporarily unavailable.")
        cost = (input_bound * input_rate + max_tokens * output_rate) / Decimal(1000000)
        with transaction.atomic():
            StudentProfile.objects.select_for_update().get(uuid=self.student_id)
            totals = ChatModelCall.objects.filter(generation__student_id=self.student_id,
                created_at__date=timezone.now().date()).aggregate(tokens=Sum("charged_tokens"), cost=Sum("estimated_cost_usd"))
            if ((totals["tokens"] or 0) + token_bound > limit("CHAT_DAILY_TOKENS", 250000) or
                (totals["cost"] or 0) + cost > Decimal(os.environ.get("CHAT_DAILY_USD", "5"))):
                raise TurnStopped("CHAT_DAILY_BUDGET", "Your daily AI allowance has been reached. Please try again tomorrow.")
            call = ChatModelCall.objects.create(generation_id=self.job_id, model=model, account_id=self.account_id, request_id=self.request_id,
                charged_tokens=token_bound, estimated_cost_usd=cost)
        return call, input_rate, output_rate


def metered_call(model, payload, max_tokens, invoke, counted_input=None):
    budget = current_budget.get()
    if budget is None:
        from django_api.telemetry import standalone_call
        return standalone_call(model, invoke)
    call, input_rate, output_rate = budget.reserve(model, payload, max_tokens, counted_input)
    started = time.monotonic()
    try:
        response = invoke()
        usage = getattr(response, "usage", None)
        if usage is None:
            generations = getattr(response, "generations", [])
            usage = getattr(generations[0].message, "usage_metadata", None) if generations else None
        if usage:
            data = usage if isinstance(usage, dict) else usage.model_dump()
            input_tokens = int(data.get("input_tokens", 0))
            output_tokens = int(data.get("output_tokens", 0))
            cache_tokens = int(data.get("cache_creation_input_tokens", 0)) + int(data.get("cache_read_input_tokens", 0))
            call.input_tokens, call.output_tokens = input_tokens + cache_tokens, output_tokens
            from django_api.telemetry import approximate_cost
            call.actual_cost_usd = approximate_cost(call.input_tokens, output_tokens, model)
            # Keep the conservative reservation charged. Unknown/time-out costs
            # never get refunded; telemetry records provider-reported usage.
            call.status = "completed"
        else:
            call.status = "usage_unknown"
        budget.check()
        return response
    except BaseException as exc:
        call.error_category = "timeout" if "timeout" in type(exc).__name__.lower() or isinstance(exc, TurnStopped) else "provider_error"
        call.status = "failed_or_unknown"
        raise
    finally:
        call.latency_ms = int((time.monotonic() - started) * 1000)
        call.save()


def anthropic_create(client, **kwargs):
    kwargs["timeout"] = CALL_SECONDS
    # Every helper uses the same no-retry client policy; no hidden retry spend.
    counted = count_multimodal_input(client.messages, kwargs)
    return metered_call(str(kwargs.get("model", "unknown")), kwargs,
        int(kwargs.get("max_tokens", 1200)), lambda: client.messages.create(**kwargs), counted_input=counted)


def count_multimodal_input(messages_api, payload):
    def has_binary(value):
        if isinstance(value, dict):
            return value.get("type") in ("image", "document") or any(has_binary(v) for v in value.values())
        if isinstance(value, list):
            return any(has_binary(v) for v in value)
        return False
    budget = current_budget.get()
    if budget is None or not has_binary(payload):
        return None
    budget.check()
    # Image tokenization is not proportional to base64 byte size. Ask the
    # provider before reserving; do not charge megabytes as text tokens.
    keys = ("model", "messages", "system", "tools", "tool_choice", "thinking")
    result = messages_api.count_tokens(**{k: payload[k] for k in keys if k in payload}, timeout=CALL_SECONDS)
    budget.check()
    return int(result.input_tokens)
