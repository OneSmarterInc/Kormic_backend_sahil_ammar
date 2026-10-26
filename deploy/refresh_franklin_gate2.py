"""Replay approved saved evidence after deploying the full-content dedup fix.

Run from the backend directory:
    ./venv/bin/python manage.py shell < deploy/refresh_franklin_gate2.py
No network crawl, source approval, secret change or old-fact deletion occurs.
"""
import hashlib
import json
from pathlib import Path

from django.db import transaction
from django_api.models import UniversityKnowledgeEntry
from knowledge.scraper import extract_facts_from_page, page_chunks
from knowledge.university_kb import UniversityKnowledgeBase
from universities.models import University
from url_discovery.models import DiscoveryJob


def refresh():
    marker = "franklin-gate2-20261026T114702Z"
    evidence_file = Path.home() / "kormic-gate2/franklin-source-review-20261026T114702Z.json"
    rows = json.loads(evidence_file.read_text(encoding="utf-8"))
    with transaction.atomic():
        university = University.objects.select_for_update().get(
            uuid="12ea2efd-73d0-4982-b022-9e76b25060f7"
        )
        job = DiscoveryJob.objects.get(
            university=university, settings__gate2_import=marker, status="completed"
        )
        hashes = job.settings["source_hashes"]
        approved = {
            url for approval in job.cluster_approvals.all() for url in approval.urls
        }
        if len(rows) != 6 or {row["url"] for row in rows} != set(hashes):
            raise ValueError("Evidence does not match the six reviewed sources")
        for row in rows:
            actual = hashlib.sha256(row["text"].encode()).hexdigest()
            if (row["url"] not in approved or not row["text"]
                    or actual != hashes[row["url"]] or actual != row["text_sha256"]
                    or len(row["text"]) != row["characters"]):
                raise ValueError("Source content or approval verification failed")
        entries = UniversityKnowledgeEntry.objects.filter(university_id=str(university.uuid))
        before = entries.count()
        kb = UniversityKnowledgeBase(str(university.uuid))
        for row in rows:
            for chunk in page_chunks(row["text"]):
                for fact in extract_facts_from_page(row["url"], chunk, university.name):
                    kb.store(topic=fact["topic"], content=fact["content"],
                             source_type="scraped", source_url=row["url"],
                             confidence=fact.get("confidence", 0.55))
            if not entries.filter(source_url=row["url"]).exists():
                raise RuntimeError("No persisted knowledge for an approved source")
        print(json.dumps({"job": job.pk, "sources": len(rows),
                          "knowledge_before": before, "knowledge_after": entries.count(),
                          "gate2": "Requires fresh ten-answer production verification"}, indent=2))


refresh()
