

from __future__ import annotations

from typing import Any, Dict, Optional

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsTOTPEnrolled, IsUniversityRole, get_account
from universities import notifications, services
from universities.identity import is_agent_name_available
from universities.knowledge_groups import ensure_default_groups, escalation_counts_by_group
from universities.models import KnowledgeGroup, University

UNIVERSITY_ADMIN_PERMISSIONS = [IsAuthenticated, IsTOTPEnrolled, IsUniversityRole]

# Profile fields that are also mirrored into the knowledge base -- editing
# any of these should re-sync the derived KB facts.
_KB_SYNCED_FIELDS = {
    "description",
    "contact_email",
    "contact_phone",
    "website_url",
    "admissions_office_address",
    "eligibility_criteria",
}

_PATCHABLE_PROFILE_FIELDS = _KB_SYNCED_FIELDS | {
    "name",
    "location",
    "tagline",
    "tone_descriptors",
    "best_fit_notes",
    "not_best_fit_notes",
    "communication_style_notes",
    "never_do_notes",
}

# Handled separately from _PATCHABLE_PROFILE_FIELDS since they need
# int/shape validation rather than a plain setattr.
_MIN_FIT_SCORE_THRESHOLD_FIELD = "min_fit_score_threshold"
_PRIORITY_TIER_BOUNDS_FIELD = "priority_tier_bounds"
_PRIORITY_TIER_KEYS = {"high", "medium", "low"}


def _error(message: str, http_status=status.HTTP_400_BAD_REQUEST) -> Response:
    return Response({"status": "error", "message": str(message)}, status=http_status)


def _get_own_university(request) -> University | None:
    account = get_account(request)
    if account is None or not account.university_id:
        return None
    return account.university


def _serialize_profile(university: University) -> Dict[str, Any]:
    return {
        "id": str(university.uuid),
        "name": university.name,
        "agent_name": university.agent_name,
        "location": university.location,
        "tagline": university.tagline,
        "description": university.description,
        "contact_email": university.contact_email,
        "contact_phone": university.contact_phone,
        "website_url": university.website_url,
        "admissions_office_address": university.admissions_office_address,
        "eligibility_criteria": university.eligibility_criteria,
        "scrape_urls": university.scrape_urls,
        "min_fit_score_threshold": university.min_fit_score_threshold,
        "priority_tier_bounds": university.priority_tier_bounds,
        "tone_descriptors": university.tone_descriptors,
        "best_fit_notes": university.best_fit_notes,
        "not_best_fit_notes": university.not_best_fit_notes,
        "communication_style_notes": university.communication_style_notes,
        "never_do_notes": university.never_do_notes,
        "setup_status": services.university_setup_status(str(university.uuid)),
        "created_at": university.created_at,
        "updated_at": university.updated_at,
    }


def _serialize_knowledge_entry(entry) -> Dict[str, Any]:
    return {
        "id": entry.id,
        "topic": entry.topic,
        "content": entry.content,
        "source_type": entry.source_type,
        "source_url": entry.source_url,
        "confidence": entry.confidence,
        "times_used": entry.times_used,
        "group": entry.group.slug if entry.group_id else None,
        "created_at": entry.created_at,
    }


def _serialize_knowledge_group(group, escalation_counts: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    counts = escalation_counts or {}
    return {
        "slug": group.slug,
        "label": group.get_slug_display(),
        "escalation_contact_name": group.escalation_contact_name,
        "escalation_contact_email": group.escalation_contact_email,
        "escalation_count": counts.get(group.slug, 0),
        "updated_at": group.updated_at,
    }


class UniversityProfileAPIView(APIView):
    """
    GET /api/university-admin/profile/
    PATCH /api/university-admin/profile/
    The setup-phase profile for the authenticated officer's own university.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)
        return Response(_serialize_profile(university))

    def patch(self, request):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        data = request.data or {}
        changed_kb_fields = False

        if _MIN_FIT_SCORE_THRESHOLD_FIELD in data:
            try:
                threshold = int(data[_MIN_FIT_SCORE_THRESHOLD_FIELD])
            except (TypeError, ValueError):
                return _error("min_fit_score_threshold must be an integer between 0 and 100.")
            if not 0 <= threshold <= 100:
                return _error("min_fit_score_threshold must be an integer between 0 and 100.")
            university.min_fit_score_threshold = threshold

        if _PRIORITY_TIER_BOUNDS_FIELD in data:
            bounds = data[_PRIORITY_TIER_BOUNDS_FIELD]
            if not isinstance(bounds, dict) or set(bounds.keys()) != _PRIORITY_TIER_KEYS:
                return _error("priority_tier_bounds must be an object with exactly 'high', 'medium', 'low' keys.")
            try:
                bounds = {key: int(value) for key, value in bounds.items()}
            except (TypeError, ValueError):
                return _error("priority_tier_bounds values must be integers between 0 and 100.")
            if not all(0 <= value <= 100 for value in bounds.values()):
                return _error("priority_tier_bounds values must be integers between 0 and 100.")
            if not (bounds["high"] >= bounds["medium"] >= bounds["low"]):
                return _error("priority_tier_bounds must satisfy high >= medium >= low.")
            university.priority_tier_bounds = bounds

        for field in _PATCHABLE_PROFILE_FIELDS:
            if field not in data:
                continue
            setattr(university, field, data[field])
            if field in _KB_SYNCED_FIELDS:
                changed_kb_fields = True

        university.save()

        if changed_kb_fields:
            services.sync_profile_facts_to_kb(university)

        return Response(_serialize_profile(university))


class UniversityProfileCompletionAPIView(APIView):
    """
    GET /api/university-admin/profile/completion/
    Standalone completion checklist for the authenticated officer's own
    university -- same derived data embedded as "setup_status" in
    UniversityProfileAPIView, exposed separately so a setup-progress widget
    doesn't need to fetch (and can't accidentally leak) the full profile.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request):
        account = get_account(request)
        if account is None or not account.university_id:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)
        return Response(services.university_setup_status(account.university_uuid))


class UniversityAgentNameAPIView(APIView):
    """
    GET /api/university-admin/agent-name/
    PATCH /api/university-admin/agent-name/  Body: {"agent_name": "..."}
    Mirrors django_api.views.AgentNameAPIView for students.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)
        return Response({"agent_name": university.agent_name})

    def patch(self, request):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        new_name = str(request.data.get("agent_name", "")).strip()

        if not new_name:
            return _error("agent_name is required.")
        if len(new_name) > 100:
            return _error("agent_name must be 100 characters or fewer.")
        if not is_agent_name_available(new_name, exclude_university_id=str(university.uuid)):
            return _error("This agent name is already taken. Please choose another.", status.HTTP_409_CONFLICT)

        university.agent_name = new_name
        university.save(update_fields=["agent_name", "updated_at"])
        return Response({"agent_name": university.agent_name})


class AutoDiscoverUrlsAPIView(APIView):

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def post(self, request):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        from url_discovery import services as discovery_services

        try:
            job = discovery_services.start_discovery(
                university,
                max_pages=request.data.get("max_pages"),
                top_n=request.data.get("top_n"),
                auto_apply=bool(request.data.get("auto_apply", True)),
            )
        except ValueError as exc:
            return _error(str(exc), status.HTTP_409_CONFLICT)
        return Response(discovery_services.serialize_job(job), status=status.HTTP_202_ACCEPTED)

    def get(self, request):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        from url_discovery import services as discovery_services

        job = university.discovery_jobs.first()
        if job is None:
            return _error("No discovery job has been run yet.", status.HTTP_404_NOT_FOUND)
        return Response(discovery_services.serialize_job(job))


class AutoDiscoverJobDetailAPIView(APIView):
    """
    GET /api/university-admin/scrape-urls/auto-discover/<job_id>/
    Status/progress for one discovery job, for polling while it runs.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request, job_id: int):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        job = university.discovery_jobs.filter(id=job_id).first()
        if job is None:
            return _error("Discovery job not found.", status.HTTP_404_NOT_FOUND)

        from url_discovery import services as discovery_services

        return Response(discovery_services.serialize_job(job))


class AutoDiscoverJobStopAPIView(APIView):
    """
    POST /api/university-admin/scrape-urls/auto-discover/<job_id>/stop/
    Cooperatively stops an in-progress crawl (checked between page fetches).
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def post(self, request, job_id: int):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        job = university.discovery_jobs.filter(id=job_id).first()
        if job is None:
            return _error("Discovery job not found.", status.HTTP_404_NOT_FOUND)

        from url_discovery import services as discovery_services

        try:
            job = discovery_services.request_stop(job)
        except ValueError as exc:
            return _error(str(exc), status.HTTP_409_CONFLICT)
        return Response(discovery_services.serialize_job(job))


class AutoDiscoverResultsAPIView(APIView):
    """
    GET /api/university-admin/scrape-urls/auto-discover/<job_id>/results/
        ?mode=student_essential (default) | base_important | all
        &limit=15
    Ranked candidate URLs from a discovery job -- the officer reviews these
    and picks which ones to apply. Available as soon as the job has crawled
    any pages; doesn't require it to be complete. limit caps how many are
    returned (defaults to the job's top_n for student_essential/base_important;
    ignored for mode=all).
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request, job_id: int):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        job = university.discovery_jobs.filter(id=job_id).first()
        if job is None:
            return _error("Discovery job not found.", status.HTTP_404_NOT_FOUND)

        from url_discovery import services as discovery_services

        mode = request.query_params.get("mode", "student_essential")
        limit_param = request.query_params.get("limit")
        try:
            limit = int(limit_param) if limit_param else None
        except ValueError:
            return _error("limit must be an integer.")

        if mode == "all":
            records = list(
                job.urls.filter(decision_status__in=["relevant", "review"]).order_by("-relevance_score")
            )
            if limit:
                records = records[:limit]
        elif mode in {"student_essential", "base_important"}:
            records = discovery_services.recommended_urls(job, mode=mode, limit=limit)
        else:
            return _error("mode must be one of: student_essential, base_important, all.")

        already_saved = set(university.scrape_urls or [])
        return Response({
            "job": discovery_services.serialize_job(job),
            "mode": mode,
            "results": [
                discovery_services.serialize_discovered_url(record, already_saved) for record in records
            ],
        })


class AutoDiscoverApplyAPIView(APIView):
    """
    POST /api/university-admin/scrape-urls/auto-discover/<job_id>/apply/
        Body: {"urls": ["https://...", ...], "replace": false}
    Merges officer-selected discovered URLs into University.scrape_urls --
    the same field PUT /scrape-urls/ edits, so manually-typed URLs are kept
    unless replace=true is explicitly passed. Scraping itself is unchanged:
    POST /scrape-urls/scrape-now/ still does the actual fact extraction.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def post(self, request, job_id: int):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        job = university.discovery_jobs.filter(id=job_id).first()
        if job is None:
            return _error("Discovery job not found.", status.HTTP_404_NOT_FOUND)

        urls = request.data.get("urls")
        if not isinstance(urls, list) or not urls:
            return _error("urls must be a non-empty list of URL strings.")

        from url_discovery import services as discovery_services

        try:
            merged = discovery_services.apply_selected_urls(
                university, job, urls, replace=bool(request.data.get("replace", False))
            )
        except ValueError as exc:
            return _error(str(exc))
        return Response({"scrape_urls": merged})


class AutoDiscoverClusterMapAPIView(APIView):
    """
    GET /api/university-admin/scrape-urls/auto-discover/<job_id>/clusters/
    B2: the proposed URL/department map for review -- this job's candidate
    pages grouped by classifier category, each showing which KnowledgeGroup
    it would feed and whether that cluster has already been approved.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request, job_id: int):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        job = university.discovery_jobs.filter(id=job_id).first()
        if job is None:
            return _error("Discovery job not found.", status.HTTP_404_NOT_FOUND)

        from url_discovery import services as discovery_services

        return Response({
            "job": discovery_services.serialize_job(job),
            "clusters": discovery_services.proposed_department_map(job),
        })


class AutoDiscoverClusterApproveAPIView(APIView):
    """
    POST /api/university-admin/scrape-urls/auto-discover/<job_id>/clusters/<category>/approve/
    B2: approves one category cluster -- applies its URLs to
    University.scrape_urls, scrapes them, tags the resulting facts with the
    mapped KnowledgeGroup, and records who/when as provenance. Re-approving
    the same cluster refreshes that provenance rather than duplicating it.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def post(self, request, job_id: int, category: str):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        job = university.discovery_jobs.filter(id=job_id).first()
        if job is None:
            return _error("Discovery job not found.", status.HTTP_404_NOT_FOUND)

        from url_discovery import services as discovery_services

        approved_by = request.user.email or request.user.get_username()

        try:
            result = discovery_services.approve_cluster(job, category, approved_by=approved_by)
        except ValueError as exc:
            return _error(str(exc), status.HTTP_404_NOT_FOUND)
        return Response(result)


class ScrapeUrlsAPIView(APIView):
    """
    GET /api/university-admin/scrape-urls/
    PUT /api/university-admin/scrape-urls/  Body: {"scrape_urls": ["https://...", ...]}
    Replaces the whole saved URL list -- knowledge.scraper.scrape_university()
    reads from this list when scrape-now is triggered.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)
        return Response({"scrape_urls": university.scrape_urls})

    def put(self, request):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        urls = request.data.get("scrape_urls")
        if not isinstance(urls, list) or not all(isinstance(url, str) and url.strip() for url in urls):
            return _error("scrape_urls must be a list of non-empty URL strings.")

        university.scrape_urls = [url.strip() for url in urls]
        university.save(update_fields=["scrape_urls", "updated_at"])
        return Response({"scrape_urls": university.scrape_urls})


class ScrapeNowAPIView(APIView):
    """
    POST /api/university-admin/scrape-urls/scrape-now/
    Queues a Celery run of the scrape-every-saved-URL pipeline and returns
    immediately -- it used to run synchronously in the request, which for a
    university with 20-30 scrape URLs (each with its own ~1.5s be-polite
    delay plus an LLM extraction call) could tie up a web worker for
    minutes. Stores extracted facts as source_type="scraped".

    Returns the queued ScrapeJob (202); poll its status at
    GET /api/university-admin/scrape-urls/scrape-now/<job_id>/.

    GET (no job_id) returns the most recent scrape job for this university,
    for a client that reloads mid-run and needs to resume polling.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def post(self, request):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        if not university.scrape_urls:
            return _error("No scrape URLs are saved yet. Save some with PUT /api/university-admin/scrape-urls/ first.")

        try:
            job = services.start_scrape_job(university)
        except ValueError as exc:
            return _error(str(exc), status.HTTP_409_CONFLICT)
        return Response(services.serialize_scrape_job(job), status=status.HTTP_202_ACCEPTED)

    def get(self, request):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        job = university.scrape_jobs.first()
        if job is None:
            return _error("No scrape job has been run yet.", status.HTTP_404_NOT_FOUND)
        return Response(services.serialize_scrape_job(job))


class ScrapeNowJobDetailAPIView(APIView):
    """
    GET /api/university-admin/scrape-urls/scrape-now/<job_id>/
    Status/result for one scrape job, for polling while it runs.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request, job_id: int):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        job = university.scrape_jobs.filter(id=job_id).first()
        if job is None:
            return _error("Scrape job not found.", status.HTTP_404_NOT_FOUND)
        return Response(services.serialize_scrape_job(job))


class KnowledgeFactListCreateAPIView(APIView):
    """
    GET /api/university-admin/knowledge/  -- every fact (any source_type) for this university.
        Optional query params narrow the list:
          ?section=<source_type>   e.g. seed, manual, scraped, conversation, human_verified
          ?source_url=<url>        exact match against the scraped page a fact came from
          ?group=<slug>            one of admissions|international|money|campus_life
    POST /api/university-admin/knowledge/  Body: {"topic": "...", "content": "...", "confidence": 1.0}
    POST always stores as source_type="manual" regardless of request body --
    the direct write path that used to only exist reactively via resolving
    a PendingQuery.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request):
        from django_api.models import UniversityKnowledgeEntry

        account = get_account(request)
        if account is None or not account.university_id:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        entries = UniversityKnowledgeEntry.objects.filter(university_id=account.university_uuid)

        section = request.query_params.get("section")
        if section:
            entries = entries.filter(source_type=section.strip().lower())

        source_url = request.query_params.get("source_url")
        if source_url:
            entries = entries.filter(source_url=source_url.strip())

        group_slug = request.query_params.get("group")
        if group_slug:
            entries = entries.filter(group__slug=group_slug.strip().lower())

        return Response({"knowledge": [_serialize_knowledge_entry(entry) for entry in entries]})

    def post(self, request):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        topic = str(request.data.get("topic", "")).strip()
        content = str(request.data.get("content", "")).strip()

        if not topic or not content:
            return _error("topic and content are required.")

        try:
            confidence = float(request.data.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0

        from django_api.models import UniversityKnowledgeEntry

        group_slug = str(request.data.get("group", "")).strip() or None
        if group_slug and group_slug not in KnowledgeGroup.Slug.values:
            return _error(f"group must be one of: {', '.join(KnowledgeGroup.Slug.values)}.")

        entry = services.add_manual_knowledge_fact(
            str(university.uuid), topic, content, confidence=confidence, group_slug=group_slug
        )
        # add_manual_knowledge_fact returns the KB wrapper's in-memory
        # KnowledgeEntry (has .db_id, not .id) -- re-fetch the real row so
        # _serialize_knowledge_entry (which expects model attributes) works
        # the same for both GET (lists model rows directly) and POST.
        row = UniversityKnowledgeEntry.objects.get(id=entry.db_id)
        return Response(_serialize_knowledge_entry(row), status=status.HTTP_201_CREATED)


class KnowledgeSectionsAPIView(APIView):
    """
    GET /api/university-admin/knowledge/sections/
    Knowledge facts grouped by section (source_type), with a count per
    section -- backs a sidebar/tab view over the flat knowledge list.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request):
        from django.db.models import Count
        from django_api.models import UniversityKnowledgeEntry

        account = get_account(request)
        if account is None or not account.university_id:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        rows = (
            UniversityKnowledgeEntry.objects.filter(university_id=account.university_uuid)
            .values("source_type")
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        return Response({
            "sections": [
                {"section": row["source_type"], "count": row["count"]} for row in rows
            ],
        })


class KnowledgeSourceUrlsAPIView(APIView):
    """
    GET /api/university-admin/knowledge/urls/
    Knowledge facts grouped by the source URL they were scraped from, with a
    count per URL -- backs a "facts by page" view. Facts with no source_url
    (seed/manual/conversation) are omitted since there is no URL to group by.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request):
        from django.db.models import Count
        from django_api.models import UniversityKnowledgeEntry

        account = get_account(request)
        if account is None or not account.university_id:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        rows = (
            UniversityKnowledgeEntry.objects.filter(university_id=account.university_uuid)
            .exclude(source_url__isnull=True)
            .exclude(source_url="")
            .values("source_url")
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        return Response({
            "urls": [
                {"source_url": row["source_url"], "count": row["count"]} for row in rows
            ],
        })


class KnowledgeFactDetailAPIView(APIView):
    """
    PATCH /api/university-admin/knowledge/<int:fact_id>/
        Body: any of {"topic": "...", "content": "...", "confidence": 1.0, "group": "money"}
        "group" accepts a slug (admissions|international|money|campus_life)
        or null/"" to clear it.
    DELETE /api/university-admin/knowledge/<int:fact_id>/
    Editing/deleting is allowed for any source_type (seed, manual, scraped,
    conversation, human_verified) -- an officer may need to correct or
    remove a fact regardless of how it entered the knowledge base.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def _get_editable_entry(self, request, fact_id: int):
        """Returns (entry, error_response). entry is None iff error_response is set."""
        from django_api.models import UniversityKnowledgeEntry

        account = get_account(request)
        if account is None or not account.university_id:
            return None, _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        entry = UniversityKnowledgeEntry.objects.filter(
            id=fact_id, university_id=account.university_uuid
        ).first()

        if entry is None:
            return None, _error("Knowledge fact not found.", status.HTTP_404_NOT_FOUND)

        return entry, None

    def patch(self, request, fact_id: int):
        entry, error = self._get_editable_entry(request, fact_id)
        if error is not None:
            return error

        data = request.data or {}
        update_fields = []

        if "topic" in data:
            topic = str(data["topic"]).strip()
            if not topic:
                return _error("topic cannot be empty.")
            entry.topic = topic
            update_fields.append("topic")

        if "content" in data:
            content = str(data["content"]).strip()
            if not content:
                return _error("content cannot be empty.")
            entry.content = content
            update_fields.append("content")

        if "confidence" in data:
            try:
                confidence = float(data["confidence"])
            except (TypeError, ValueError):
                return _error("confidence must be a number.")
            entry.confidence = max(0.0, min(1.0, confidence))
            update_fields.append("confidence")

        if "group" in data:
            raw_group = data["group"]
            if raw_group in (None, ""):
                entry.group = None
                update_fields.append("group")
            else:
                group_slug = str(raw_group).strip().lower()
                if group_slug not in KnowledgeGroup.Slug.values:
                    return _error(f"group must be one of: {', '.join(KnowledgeGroup.Slug.values)}.")

                university = University.objects.filter(uuid=entry.university_id).first()
                if university is not None:
                    ensure_default_groups(university)

                group = KnowledgeGroup.objects.filter(
                    university__uuid=entry.university_id, slug=group_slug
                ).first()
                if group is None:
                    return _error("Knowledge group not found.", status.HTTP_404_NOT_FOUND)

                entry.group = group
                update_fields.append("group")

        if not update_fields:
            return _error("Provide at least one of topic, content, confidence, group to update.")

        entry.save(update_fields=update_fields)
        return Response(_serialize_knowledge_entry(entry))

    def delete(self, request, fact_id: int):
        entry, error = self._get_editable_entry(request, fact_id)
        if error is not None:
            return error

        entry.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class KnowledgeGroupListAPIView(APIView):
    """
    GET /api/university-admin/knowledge-groups/
  
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        ensure_default_groups(university)
        counts = escalation_counts_by_group(str(university.uuid))
        groups = university.knowledge_groups.all()

        return Response({
            "groups": [_serialize_knowledge_group(group, counts) for group in groups],
        })


class KnowledgeGroupDetailAPIView(APIView):
    """
    PATCH /api/university-admin/knowledge-groups/<slug>/
        Body: any of {"escalation_contact_name": "...", "escalation_contact_email": "..."}
    Updates one group's escalation contact. 404s if the group doesn't exist
    yet for this university -- GET the list first to lazily create the four
    defaults.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def patch(self, request, slug: str):
        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        group = university.knowledge_groups.filter(slug=slug).first()
        if group is None:
            return _error("Knowledge group not found.", status.HTTP_404_NOT_FOUND)

        data = request.data or {}
        update_fields = []

        if "escalation_contact_name" in data:
            group.escalation_contact_name = str(data["escalation_contact_name"]).strip()
            update_fields.append("escalation_contact_name")

        if "escalation_contact_email" in data:
            group.escalation_contact_email = str(data["escalation_contact_email"]).strip()
            update_fields.append("escalation_contact_email")

        if not update_fields:
            return _error("Provide at least one of escalation_contact_name, escalation_contact_email to update.")

        update_fields.append("updated_at")
        group.save(update_fields=update_fields)

        counts = escalation_counts_by_group(str(university.uuid))
        return Response(_serialize_knowledge_group(group, counts))


class KnowledgeGroupFactsAPIView(APIView):
    """
    GET /api/university-admin/knowledge-groups/<slug>/knowledge_list/
    Knowledge facts tagged with one group -- backs a "view list" action per
    group on the officer dashboard. Equivalent to
    GET /knowledge/?group=<slug> but also returns the group's own record
    (contact info, escalation_count) alongside its facts in one call.

    Only manually-added facts are ever tagged to a group (see
    add_manual_knowledge_fact / KnowledgeFactDetailAPIView) -- auto-scrape
    and URL discovery do not route facts here. No email is sent for this
    endpoint; escalation routing and its notification live on the sibling
    .../escalations_list/ endpoint.

    Lazily creates the 4 default groups first, same as the group list GET,
    so this works even if the frontend hasn't called that endpoint yet.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request, slug: str):
        from django_api.models import UniversityKnowledgeEntry

        university = _get_own_university(request)
        if university is None:
            return _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

        slug = slug.strip().lower()
        if slug not in KnowledgeGroup.Slug.values:
            return _error(
                f"slug must be one of: {', '.join(KnowledgeGroup.Slug.values)}.",
                status.HTTP_404_NOT_FOUND,
            )

        ensure_default_groups(university)
        group = university.knowledge_groups.filter(slug=slug).first()

        entries = UniversityKnowledgeEntry.objects.filter(university_id=str(university.uuid), group__slug=slug)
        counts = escalation_counts_by_group(str(university.uuid))

        return Response({
            "group": _serialize_knowledge_group(group, counts),
            "knowledge": [_serialize_knowledge_entry(entry) for entry in entries],
        })


def _resolve_group_or_error(request, slug: str):
    """Shared slug validation + lazy-group-creation for the two escalation
    views below. Returns (university, group, error_response); university and
    group are None iff error_response is set."""
    university = _get_own_university(request)
    if university is None:
        return None, None, _error("No university profile found for this account.", status.HTTP_404_NOT_FOUND)

    slug = slug.strip().lower()
    if slug not in KnowledgeGroup.Slug.values:
        return None, None, _error(
            f"slug must be one of: {', '.join(KnowledgeGroup.Slug.values)}.",
            status.HTTP_404_NOT_FOUND,
        )

    ensure_default_groups(university)
    group = university.knowledge_groups.filter(slug=slug).first()
    if group is None:
        return None, None, _error("Knowledge group not found.", status.HTTP_404_NOT_FOUND)

    return university, group, None


class KnowledgeGroupEscalationsAPIView(APIView):
    """
    GET /api/university-admin/knowledge-groups/<slug>/escalations_list/
    The actual list behind a group's "escalation_count" -- every escalated
    student question (PendingQuery) routed to this group, newest first,
    plus the group's own record. This is the group's real payload: a
    knowledge group exists to collect escalations and route them to a named
    contact (who is emailed automatically the moment an escalation lands
    here -- see universities.notifications.send_escalation_routed_alert).
    Group-tagged knowledge facts are a separate concern, served by the
    sibling .../knowledge_list/ endpoint.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request, slug: str):
        from django_api.models import PendingQuery
        from django_api.views import serialize_pending_query

        university, group, error = _resolve_group_or_error(request, slug)
        if error is not None:
            return error

        escalations = PendingQuery.objects.filter(
            university_id=str(university.uuid), group__slug=group.slug
        ).order_by("-created_at")

        counts = escalation_counts_by_group(str(university.uuid))

        return Response({
            "group": _serialize_knowledge_group(group, counts),
            "escalations": [serialize_pending_query(query) for query in escalations],
        })


class KnowledgeGroupEscalationNotifyAPIView(APIView):
    """
    POST /api/university-admin/knowledge-groups/<slug>/escalations_list/notify/
        Body (optional): {"escalation_ids": [1, 2], "message": "..."}
    Manual re-send of the group's escalation digest. Escalations are already
    emailed to the contact automatically as they arrive (see
    send_escalation_routed_alert); this is the officer-triggered "email them
    again / email this selection with a note" action. Defaults to every
    still-pending escalation for the group; pass escalation_ids to scope the
    email to a specific selection instead (ids outside this group/university
    are silently ignored, not errored, so a stale id in a batch doesn't fail
    the whole send). "message" is an optional note prepended to the
    auto-generated summary.
    """

    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def post(self, request, slug: str):
        from django_api.models import PendingQuery

        university, group, error = _resolve_group_or_error(request, slug)
        if error is not None:
            return error

        if not group.escalation_contact_email:
            return _error(
                "This group has no escalation contact email set. Set one first with "
                "PATCH /api/university-admin/knowledge-groups/<slug>/."
            )

        data = request.data or {}
        escalation_ids = data.get("escalation_ids")

        escalations_qs = PendingQuery.objects.filter(university_id=str(university.uuid), group__slug=group.slug)
        if escalation_ids not in (None, ""):
            if not isinstance(escalation_ids, list) or not all(isinstance(i, int) for i in escalation_ids):
                return _error("escalation_ids must be a list of integer ids.")
            escalations_qs = escalations_qs.filter(id__in=escalation_ids)
        else:
            escalations_qs = escalations_qs.filter(status=PendingQuery.Status.PENDING)

        escalations = list(escalations_qs.order_by("-created_at"))
        if not escalations:
            return _error("No matching escalations to notify about.", status.HTTP_404_NOT_FOUND)

        custom_message = str(data.get("message", "")).strip()

        sent = notifications.send_escalation_digest_email(
            to_email=group.escalation_contact_email,
            to_name=group.escalation_contact_name,
            group_label=group.get_slug_display(),
            university_name=university.name,
            escalations=escalations,
            custom_message=custom_message,
        )

        if not sent:
            return _error(
                "Failed to send the notification email. Please try again shortly.",
                status.HTTP_502_BAD_GATEWAY,
            )

        return Response({
            "status": "sent",
            "to": group.escalation_contact_email,
            "escalation_count": len(escalations),
            "escalation_ids": [query.id for query in escalations],
        })
