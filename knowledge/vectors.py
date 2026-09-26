"""University-scoped pgvector retrieval with local, CPU-only embeddings.

Claude remains the answer model. No knowledge is sent to an embedding API.
SQLite development uses the existing lexical search instead.
"""
from __future__ import annotations

import hashlib
import logging
from functools import lru_cache
from threading import Lock

from django.conf import settings
from django.db import connection, transaction
from pgvector.django import CosineDistance

logger = logging.getLogger(__name__)
MODEL = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384
_model_lock = Lock()


@lru_cache(maxsize=1)
def _embedding_model():
    from fastembed import TextEmbedding

    return TextEmbedding(
        model_name=MODEL,
        cache_dir=str(settings.UNIVERSITY_EMBEDDING_CACHE_DIR),
        threads=2,
    )


def embed_texts(texts, *, query=False):
    # Loading and encoding are bounded to one CPU session per process.
    with _model_lock:
        model = _embedding_model()
        vectors = model.query_embed(texts) if query else model.passage_embed(texts)
        return [vector.tolist() for vector in vectors]


def enabled():
    return connection.vendor == "postgresql" and settings.UNIVERSITY_VECTOR_SEARCH


def sync_embeddings(university_id):
    """Index new/edited facts, including direct ORM edits and old scraped data.

    Compare content hashes on every sync so bulk updates cannot leave stale
    vectors. Conditional writes avoid assigning an old vector to a new edit.
    """
    from django_api.models import UniversityKnowledgeEntry

    if not enabled():
        return 0
    rows = UniversityKnowledgeEntry.objects.filter(university_id=str(university_id)).defer("embedding")
    pending = []
    for row in rows.iterator():
        text = f"{row.topic}\n{row.content}"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if row.embedding_hash != digest or row.embedding_model != MODEL:
            pending.append((row, text, digest))
    stored = 0
    for start in range(0, len(pending), 32):
        batch = pending[start:start + 32]
        vectors = embed_texts([text for _, text, _ in batch])
        for (row, _, digest), vector in zip(batch, vectors, strict=True):
            stored += UniversityKnowledgeEntry.objects.filter(
                pk=row.pk, university_id=str(university_id), topic=row.topic, content=row.content,
            ).update(embedding=vector, embedding_hash=digest, embedding_model=MODEL)
    return stored


def search_ids(university_id, query, limit=8):
    """Filter by university BEFORE cosine ranking; never search other tenants."""
    from django_api.models import UniversityKnowledgeEntry

    if not enabled() or not query.strip():
        return []
    try:
        # A savepoint lets lexical retrieval continue even after a DB error.
        with transaction.atomic():
            # Document vectors are maintained by ingestion workers. Never scan
            # or encode the university corpus while answering a chat request.
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            vector = embed_texts([query], query=True)[0]
            return list(
                UniversityKnowledgeEntry.objects.filter(
                    university_id=str(university_id), embedding_model=MODEL,
                    embedding__isnull=False,
                )
                .annotate(distance=CosineDistance("embedding", vector))
                .filter(distance__lt=0.65)
                .order_by("distance")
                .values_list("id", flat=True)[:limit]
            )
    except Exception:
        logger.exception("Vector retrieval unavailable for university %s; using lexical search", university_id)
        return []
