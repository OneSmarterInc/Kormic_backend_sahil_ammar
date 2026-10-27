"""Import the operator-approved Franklin notice with actual fetch provenance."""
import hashlib
import json

import httpx
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from knowledge.scraper import HEADERS, fetch_page, page_chunks
from knowledge.university_kb import UniversityKnowledgeBase
from universities.models import KnowledgeGroup, University
from url_discovery.domain_policy import DomainPolicy
from url_discovery.models import DiscoveryClusterApproval, DiscoveredUrl, DiscoveryJob
from url_discovery.safe_fetch import request_with_policy


URL = "https://www.franklin.edu/tuition-fee-changes-fall-2026"
SCHOOL = "12ea2efd-73d0-4982-b022-9e76b25060f7"
CATEGORY = "Fees, Tuition & Financial Aid"


class Command(BaseCommand):
    help = "Fetch and load Franklin's approved Fall 2026 notice; no LLM or broad crawl."

    def add_arguments(self, parser):
        parser.add_argument("--approved-by", required=True, help="Name of the approving production operator")

    def handle(self, *args, **options):
        approver = options["approved_by"].strip()
        if not approver or len(approver) > 255:
            raise CommandError("Provide an operator name of 1-255 characters.")
        university = University.objects.filter(uuid=SCHOOL).first()
        if university is None or university.name.casefold().strip() != "franklin university":
            raise CommandError("Expected Franklin University record was not found.")
        group = KnowledgeGroup.objects.filter(university=university, slug="money").first()
        if group is None:
            raise CommandError("Franklin's Money group must exist before import.")

        policy = DomainPolicy(URL, include_subdomains=True)
        try:
            with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=False) as client:
                snapshot = request_with_policy(client, URL, policy)
                status, headers, body, final_url = snapshot
                if status != 200 or final_url.rstrip("/") != URL:
                    raise CommandError(f"Expected the notice itself with HTTP 200; received {status} at {final_url}.")
                if "html" not in headers.get("content-type", "").lower():
                    raise CommandError("Expected an HTML policy page.")
                text = fetch_page(URL, domain_policy=policy, client=client, snapshot=snapshot)
        except (httpx.HTTPError, ValueError) as exc:
            raise CommandError(f"Fetch failed; no knowledge written: {exc}") from exc
        lowered = text.casefold()
        if not all(term in lowered for term in ("fall 2026", "tuition guarantee", "master")):
            raise CommandError("Response does not contain the expected policy sections; no knowledge written.")

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        checked_at = timezone.now()
        marker = "franklin-fall2026-" + digest
        # Serialize repeated imports; identical content reuses its provenance.
        with transaction.atomic():
            university = University.objects.select_for_update().get(pk=university.pk)
            job = university.discovery_jobs.filter(settings__operator_import=marker).first()
            if job is None:
                job = DiscoveryJob.objects.create(
                    university=university, base_url=URL, root_domain="franklin.edu",
                    allowed_domains=["www.franklin.edu"], auto_apply=False, top_n=1,
                    status="completed", started_at=checked_at, completed_at=checked_at,
                    pages_discovered=1, pages_crawled=1, relevant_count=1,
                    settings={"operator_import": marker, "text_sha256": digest,
                              "text_snapshot": text, "approved_by": approver},
                )
                DiscoveredUrl.objects.create(
                    job=job, original_url=URL, normalized_url=URL, final_url=final_url,
                    http_status=status, content_type=headers.get("content-type", ""),
                    crawled_at=checked_at, html_snapshot=body.decode("utf-8", errors="replace"),
                    content_hash=hashlib.sha256(body).hexdigest(),
                    primary_category=CATEGORY, decision_status="relevant",
                    is_discovery_only=False,
                    classification_reason="Explicit production operator approval of this tuition policy URL.",
                )
                DiscoveryClusterApproval.objects.create(
                    job=job, category=CATEGORY, knowledge_group=group, urls=[URL],
                    url_count=1, approved_by=approver, approved_at=checked_at,
                )
            kb = UniversityKnowledgeBase(str(university.uuid))
            ids = []
            for index, chunk in enumerate(page_chunks(text), 1):
                entry = kb.store(
                    topic=f"Tuition + Fee Changes Effective Fall 2026 - source section {index}",
                    content=chunk, source_type="scraped", source_url=URL, confidence=0.9,
                )
                ids.append(entry.db_id)
            if not ids or not all(ids):
                raise CommandError("No persisted policy sections; import rolled back.")
        self.stdout.write(json.dumps({"source_url": URL, "discovery_job": job.pk,
            "knowledge_ids": ids, "sections": len(ids), "text_sha256": digest,
            "checked_at": checked_at.isoformat(), "status": "loaded; retest Question 5"}, indent=2))
