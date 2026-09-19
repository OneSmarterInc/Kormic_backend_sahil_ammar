"""Hybrid reciprocal-rank fusion; citations come from stored evidence, never model URLs."""
import hashlib
import math
import os
from datetime import timedelta
import requests
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from pgvector.django import CosineDistance
from django_api.models import KnowledgeChunk, KnowledgeSource, UniversityKnowledgeEntry

MODEL = os.getenv("KNOWLEDGE_EMBEDDING_MODEL", "voyage-4-lite")
HUMAN = {"human_verified", "verified"}
SCRAPED = {"scraped", "official_page", "fallback_scrape"}


def embed(texts, input_type):
    key = os.getenv("VOYAGE_API_KEY")
    if not key:
        raise RuntimeError("EMBEDDINGS_NOT_CONFIGURED")
    from types import SimpleNamespace
    from django_api.chat_policy import metered_call
    def invoke():
        response = requests.post("https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={"input": texts, "model": MODEL, "input_type": input_type, "output_dimension": 1024},
            timeout=(3, 10))
        response.raise_for_status()
        payload = response.json()
        rows = sorted(payload["data"], key=lambda row: row["index"])
        vectors = [row["embedding"] for row in rows]
        if len(vectors) != len(texts) or any(len(v) != 1024 or not all(math.isfinite(x) for x in v) or not any(v) for v in vectors):
            raise ValueError("INVALID_EMBEDDING_RESPONSE")
        tokens = payload.get("usage", {}).get("total_tokens")
        return SimpleNamespace(vectors=vectors, usage={"input_tokens": tokens, "output_tokens": 0} if type(tokens) is int else None)
    return metered_call(MODEL, texts, 0, invoke).vectors


def sync_chunks(entry):
    with transaction.atomic():
        # Serialize concurrent indexing and don't recreate chunks for a deleted entry.
        entry = UniversityKnowledgeEntry.objects.select_for_update().filter(pk=entry.pk).first()
        if entry is None:
            return
        text = f"{entry.topic}\n{entry.content}"
        parts = [text[i:i + 1800] for i in range(0, len(text), 1600)]
        for ordinal, part in enumerate(parts):
            digest = hashlib.sha256(part.encode()).hexdigest()
            row, created = KnowledgeChunk.objects.get_or_create(entry=entry, ordinal=ordinal,
                defaults={"text": part, "content_hash": digest})
            if not created and row.content_hash != digest:
                row.text, row.content_hash, row.embedding, row.embedding_model, row.embedded_at = part, digest, None, "", None
                row.save()
        entry.chunks.filter(ordinal__gte=len(parts)).delete()


def index_pending(batch_size=64):
    # This also repairs changes made by bulk-update code paths and model switches.
    for entry in UniversityKnowledgeEntry.objects.filter(active=True).iterator(chunk_size=200):
        sync_chunks(entry)
    if not os.getenv("VOYAGE_API_KEY"):
        return {"indexed": 0, "status": "embeddings_not_configured"}
    rows = list(KnowledgeChunk.objects.filter(entry__active=True).filter(Q(embedding__isnull=True) | ~Q(embedding_model=MODEL)).order_by("pk")[:batch_size])
    if not rows:
        return {"indexed": 0, "status": "ready"}
    vectors = embed([r.text for r in rows], "document")
    count = 0
    for row, vector in zip(rows, vectors):
        count += KnowledgeChunk.objects.filter(pk=row.pk, content_hash=row.content_hash).update(
            embedding=vector, embedding_model=MODEL, embedded_at=timezone.now())
    return {"indexed": count, "status": "ready"}


def source_metadata(university_id):
    return {r.url: r for r in KnowledgeSource.objects.filter(university__uuid=university_id)}


def available(entry, sources):
    if not getattr(entry, "active", True):
        return False
    if entry.source_type not in SCRAPED:
        return True
    source = sources.get(entry.source_url)
    # Existing legacy facts without a tracked source are withheld until recrawled.
    return bool(source and source.enabled and source.health not in {"deleted", "robots_blocked", "changed_pending"}
                and source.last_success_at and source.last_success_at + timedelta(days=source.stale_days) > timezone.now())


def hybrid_search(kb, query, limit=8):
    sources = source_metadata(kb.university_id)
    candidates = [e for e in kb.entries if available(e, sources)]
    words = kb._tokenize(query)
    lexical = []
    for entry in candidates:
        score = kb._phrase_score(query, entry.topic, entry.content)
        for word in words:
            score += (10 if word in kb.IMPORTANT_WORDS else 3) if word in entry.topic.lower() else 0
            score += (5 if word in kb.IMPORTANT_WORDS else 1) if word in entry.content.lower() else 0
        if score > 0:
            lexical.append((score, entry.db_id))
    lexical.sort(reverse=True)
    ranks = {pk: 1 / (60 + i) for i, (_, pk) in enumerate(lexical, 1)}
    ids = [e.db_id for e in candidates if e.db_id]
    if ids and connection.vendor == "postgresql" and os.getenv("VOYAGE_API_KEY"):
        try:
            rows = KnowledgeChunk.objects.filter(entry_id__in=ids, embedding_model=MODEL, embedding__isnull=False)
            if rows.exists():
                vector = embed([query[:8000]], "query")[0]
                nearest = rows.annotate(distance=CosineDistance("embedding", vector)).filter(distance__lt=0.65).order_by("distance")[:40]
                seen = set()
                for row in nearest:
                    if row.entry_id in seen: continue
                    seen.add(row.entry_id)
                    ranks[row.entry_id] = ranks.get(row.entry_id, 0) + 1 / (60 + len(seen))
        except (requests.RequestException, ValueError, KeyError, RuntimeError):
            # Provider outage cannot remove lexical evidence or invent relevance.
            pass
    results = []
    for entry in candidates:
        if entry.db_id not in ranks: continue
        source = sources.get(entry.source_url)
        age = max(0, (timezone.now() - source.last_success_at).total_seconds() / 86400) if source and source.last_success_at else 0
        freshness = 1 / (1 + age / max(source.stale_days, 1)) if source else 1
        entry.search_score = ranks[entry.db_id] * kb.SOURCE_PRIORITY.get(entry.source_type, 0.5) * entry.confidence * freshness
        results.append(entry)
    results.sort(key=lambda e: (-e.search_score, e.db_id or 0))
    return results[:limit]


def citations(entries, university_id):
    sources = source_metadata(university_id)
    result = []
    for entry in entries:
        source = sources.get(entry.source_url)
        verified = getattr(entry, "last_verified_at", None)
        result.append({"id": f"knowledge:{entry.db_id}", "title": entry.topic,
            "url": entry.source_url if str(entry.source_url or "").startswith(("https://", "http://")) else None,
            "source_type": entry.source_type, "human_verified": entry.source_type in HUMAN,
            "last_verified_at": verified.isoformat() if verified else None,
            "last_fetched_at": source.last_success_at.isoformat() if source and source.last_success_at else None})
    return result
