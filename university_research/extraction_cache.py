"""Versioned, source-backed public-page extractions shared across students."""

import hashlib
import json
import re
from contextlib import contextmanager
from copy import deepcopy

from django.db import connection
from django.utils import timezone

from .models import PublicPageExtraction


@contextmanager
def extraction_lock(key):
    """Single-flight identical extractions across PostgreSQL worker processes."""
    if connection.vendor != 'postgresql':
        yield
        return
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).digest()
    lock_id = int.from_bytes(digest[:8], 'big', signed=True)
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_lock(%s)', [lock_id])
        try:
            yield
        finally:
            cursor.execute('SELECT pg_advisory_unlock(%s)', [lock_id])


def versioned_hash(label, value):
    """Changing a schema or prompt invalidates its prior extraction automatically."""
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return f'{label}:{hashlib.sha256(value.encode()).hexdigest()[:32]}'


def source_excerpts(content, result):
    """Retain short verbatim source spans most relevant to extracted values."""
    values = []
    quotes = []

    def collect(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == 'source_quote' and isinstance(value, str) and value in content:
                    quotes.append(value[:500])
                elif key != 'confidence':
                    collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)
        elif isinstance(node, str) and node.strip() and node.strip().lower() != 'n/a':
            values.append(node.strip().lower())

    collect(result)
    spans = []
    for line in re.split(r'\n+|(?<=[.!?])\s+', content):
        line = line.strip()
        if not line:
            continue
        for start in range(0, len(line), 500):
            spans.append(line[start:start + 500])
    ranked = sorted(enumerate(spans), key=lambda pair: (
        -sum(3 if value in pair[1].lower() else 1 if any(
            number in pair[1] for number in re.findall(r'\d[\d,.]*', value)) else 0
            for value in values[:100]), pair[0]))
    return list(dict.fromkeys([*quotes, *(span for _, span in ranked)]))[:12]


def get_or_extract(*, content, schema_version, instructions_version, model_version,
                   source_url, retrieved_at=None, extract, validate):
    """Cache only validated public evidence, never personalized eligibility."""
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    key = dict(content_hash=content_hash, schema_version=schema_version,
               instructions_version=instructions_version, model_version=model_version)
    cached = PublicPageExtraction.objects.filter(**key).first()
    if cached is not None:
        # Return copies so callers cannot mutate a cached result in this turn.
        return deepcopy(cached.result), True
    with extraction_lock(key):
        cached = PublicPageExtraction.objects.filter(**key).first()
        if cached is not None:
            return deepcopy(cached.result), True
        result = extract()
        validate(result)
        saved, created = PublicPageExtraction.objects.get_or_create(**key, defaults={
            'result': result, 'source_url': source_url,
            'retrieved_at': retrieved_at or timezone.now(),
            'supporting_excerpts': source_excerpts(content, result),
        })
        return deepcopy(result if created else saved.result), not created
