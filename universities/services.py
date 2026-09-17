
from __future__ import annotations

from typing import Any, Dict, List, Optional

from personas.university_persona_builder import build_constitution
from universities.identity import ensure_agent_name
from universities.models import University


PROGRAM_OVERVIEW_TOPIC = "Program Overview"
CONTACT_INFO_TOPIC = "Contact Information"


def build_persona_dict(university: University) -> Dict[str, Any]:
    """Adapt a University row into the exact shape
    personas.university_personas.UNIVERSITY_PERSONAS[id] used to have, so
    agents.university_agent.UniversityAgent needs no further changes."""
    constitution = build_constitution(
        agent_name=university.agent_name or str(university.uuid),
        program_name=university.name,
        location=university.location,
        tagline=university.tagline,
        description=university.description,
        tone_descriptors=university.tone_descriptors,
        best_fit_notes=university.best_fit_notes,
        not_best_fit_notes=university.not_best_fit_notes,
        communication_style_notes=university.communication_style_notes,
        never_do_notes=university.never_do_notes,
    )

    return {
        "name": university.name,
        "agent_name": university.agent_name or str(university.uuid),
        "location": university.location,
        "tagline": university.tagline,
        "constitution": constitution,
        "scrape_urls": list(university.scrape_urls or []),
        # Seed/manual/scraped facts live only in UniversityKnowledgeEntry
        # (already DB-backed) -- never re-derived from the persona dict.
        "key_facts_seed": [],
    }


def register_university(institution_name: str) -> University:
    """Create a bare University row with an auto-generated unique id and
    agent name -- the registration-time half of the two-phase flow. Setup
    (description/contacts/eligibility/scrape URLs/knowledge) all happens
    afterward via the universities-admin endpoints."""
    university = University.objects.create(name=institution_name.strip())
    ensure_agent_name(university)
    return university


def _kb_for(university_id: str):
    from knowledge.university_kb import UniversityKnowledgeBase

    return UniversityKnowledgeBase(university_id)


def _already_scraped_urls(university_id: str) -> set[str]:
    """Normalized source_url values already represented in this
    university's knowledge base. Used to skip re-scraping a page whose
    content is already captured -- without this, clicking "scrape now" or
    approving the same cluster twice re-fetches the page and asks Claude to
    re-extract facts, which (worded slightly differently each time) slip
    past the exact-match dedup in UniversityKnowledgeBase.store() and pile
    up as near-duplicate facts in the same section."""
    from url_discovery.url_normalizer import normalize_url

    from django_api.models import UniversityKnowledgeEntry

    raw_urls = (
        UniversityKnowledgeEntry.objects.filter(university_id=university_id)
        .exclude(source_url__isnull=True)
        .exclude(source_url="")
        .values_list("source_url", flat=True)
    )

    return {normalized for u in raw_urls if (normalized := normalize_url(u))}


def _dedupe_urls(urls: List[str]) -> List[str]:
    """Order-preserving de-dup of a URL list by normalized form, so a URL
    saved twice (e.g. with/without a trailing slash) is only ever fetched
    once per call."""
    from url_discovery.url_normalizer import normalize_url

    seen: set[str] = set()
    deduped: List[str] = []
    for url in urls:
        normalized = normalize_url(url) or url
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def sync_profile_facts_to_kb(university: University) -> None:
    """Project description/contacts/eligibility_criteria into
    UniversityKnowledgeEntry rows (source_type="seed") so the agent can
    actually answer from them, not just recite them in its constitution.
    Upserts by fixed topic name since KB.store() only dedups by exact
    (topic, content)."""
    from django_api.models import UniversityKnowledgeEntry

    university_id = str(university.uuid)
    kb = _kb_for(university_id)
    topics_to_replace: List[str] = [PROGRAM_OVERVIEW_TOPIC, CONTACT_INFO_TOPIC]

    eligibility_topics = [
        f"Eligibility: {str(item.get('criterion', '')).strip()}"
        for item in (university.eligibility_criteria or [])
        if isinstance(item, dict) and str(item.get("criterion", "")).strip()
    ]
    topics_to_replace.extend(eligibility_topics)

    UniversityKnowledgeEntry.objects.filter(university_id=university_id, topic__in=topics_to_replace).delete()
    # The in-memory KB instance was loaded before the delete above; drop its
    # cached copies of these topics too so store()'s duplicate-detection
    # doesn't resurrect a stale entry instead of writing the new content.
    kb.entries = [entry for entry in kb.entries if entry.topic not in topics_to_replace]

    if university.description:
        kb.store(
            topic=PROGRAM_OVERVIEW_TOPIC,
            content=university.description,
            source_type="seed",
            confidence=1.0,
        )

    contact_parts = []
    if university.contact_email:
        contact_parts.append(f"Email: {university.contact_email}")
    if university.contact_phone:
        contact_parts.append(f"Phone: {university.contact_phone}")
    if university.website_url:
        contact_parts.append(f"Website: {university.website_url}")
    if university.admissions_office_address:
        contact_parts.append(f"Admissions office: {university.admissions_office_address}")

    if contact_parts:
        kb.store(
            topic=CONTACT_INFO_TOPIC,
            content=" | ".join(contact_parts),
            source_type="seed",
            confidence=1.0,
        )

    for item in university.eligibility_criteria or []:
        if not isinstance(item, dict):
            continue
        criterion = str(item.get("criterion", "")).strip()
        detail = str(item.get("detail", "")).strip()
        if not criterion:
            continue
        kb.store(
            topic=f"Eligibility: {criterion}",
            content=detail or criterion,
            source_type="seed",
            confidence=1.0,
        )

from universities.models import ScrapeJob
from universities.tasks import run_scrape_now_job
def start_scrape_job(university: University) -> "ScrapeJob":
    """Queue a Celery run of scrape_now() for this university instead of
    running it in the request. Raises ValueError if a run is already in
    progress, mirroring start_discovery's guard against duplicate jobs."""

    active = university.scrape_jobs.filter(status__in=ScrapeJob.ACTIVE_STATUSES).first()
    if active:
        raise ValueError(f"A scrape is already in progress for this university (job {active.id}).")

    job = ScrapeJob.objects.create(university=university)
    run_scrape_now_job.delay(job.id)
    return job


def serialize_scrape_job(job: "ScrapeJob") -> Dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "result": job.result,
        "error_message": job.error_message,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "created_at": job.created_at,
    }


_ALREADY_SCRAPED_REASON = (
    "Skipped -- this URL's content is already captured in the knowledge base. "
    "Edit or delete the existing fact(s) first if the page has changed and "
    "needs to be re-scraped."
)


def scrape_now(university: University) -> Dict[str, Any]:
    """Synchronously scrape every saved URL, one at a time, so a failure on
    one page doesn't lose results from the others. knowledge.scraper's
    scrape_university() signature is untouched -- this just calls it once
    per URL and aggregates. It already sleeps ~1.5s per URL internally, so
    looping single-URL calls costs nothing extra in wall time versus one
    batched call, and buys per-URL visibility.

    URLs already represented in the knowledge base (by normalized source_url)
    are skipped rather than re-scraped, so repeated clicks don't keep adding
    near-duplicate facts extracted from the same page with slightly
    different wording each time."""
    from knowledge.scraper import scrape_university

    university_id = str(university.uuid)
    urls = _dedupe_urls(list(university.scrape_urls or []))
    kb = _kb_for(university_id)
    already_scraped = _already_scraped_urls(university_id)

    results: List[Dict[str, Any]] = []
    for url in urls:
        if url in already_scraped:
            results.append({"url": url, "status": "skipped", "facts_stored": 0, "reason": _ALREADY_SCRAPED_REASON})
            continue
        try:
            count = scrape_university(university_id, [url], university.name, kb)
            results.append({"url": url, "status": "ok", "facts_stored": count})
            if count:
                already_scraped.add(url)
        except Exception as exc:
            results.append({"url": url, "status": "failed", "facts_stored": 0, "error": str(exc)})

    return {
        "total_facts_stored": sum(r["facts_stored"] for r in results),
        "results": results,
    }


def scrape_selected_urls(university: University, urls: List[str], group_id: Optional[int] = None) -> Dict[str, Any]:
    """Same one-URL-at-a-time loop as scrape_now(), but for an explicit URL
    subset instead of every saved scrape_url.

    group_id is retained only as a low-level passthrough to scrape_university;
    no caller sets it anymore. Auto-scraped facts (including approve_cluster's
    path) are deliberately left ungrouped -- a knowledge group collects
    escalations, not scraped knowledge, and only a manual fact add routes
    into one.

    Same already-scraped skip as scrape_now() -- this is also the path
    approve_cluster() calls, so re-approving a cluster (or a cluster whose
    URLs overlap one already scraped) doesn't re-ingest the same content."""
    from knowledge.scraper import scrape_university

    university_id = str(university.uuid)
    urls = _dedupe_urls(list(urls))
    kb = _kb_for(university_id)
    already_scraped = _already_scraped_urls(university_id)

    results: List[Dict[str, Any]] = []
    for url in urls:
        if url in already_scraped:
            results.append({"url": url, "status": "skipped", "facts_stored": 0, "reason": _ALREADY_SCRAPED_REASON})
            continue
        try:
            count = scrape_university(university_id, [url], university.name, kb, group_id=group_id)
            results.append({"url": url, "status": "ok", "facts_stored": count})
            if count:
                already_scraped.add(url)
        except Exception as exc:
            results.append({"url": url, "status": "failed", "facts_stored": 0, "error": str(exc)})

    return {
        "total_facts_stored": sum(r["facts_stored"] for r in results),
        "results": results,
    }


def add_manual_knowledge_fact(
    university_id: str,
    topic: str,
    content: str,
    confidence: float = 1.0,
    source_url: Optional[str] = None,
    group_slug: Optional[str] = None,
) -> "Any":
    """Admin-entered fact, always stored as source_type="manual" regardless
    of caller input -- the direct write path that used to only exist
    reactively via resolving a PendingQuery."""
    group_id = None
    if group_slug:
        from universities.models import KnowledgeGroup

        group_id = (
            KnowledgeGroup.objects.filter(university__uuid=university_id, slug=group_slug)
            .values_list("id", flat=True)
            .first()
        )

    kb = _kb_for(university_id)
    return kb.store(
        topic=topic,
        content=content,
        source_type="manual",
        source_url=source_url,
        confidence=confidence,
        group_id=group_id,
    )


_COMPLETION_STEPS = [
    ("has_description", "Add a program description."),
    ("has_contacts", "Add at least one contact detail (email, phone, website, or address)."),
    ("has_eligibility_criteria", "Add eligibility criteria."),
    ("has_scrape_urls", "Save at least one official page URL to scrape."),
    ("has_knowledge_facts", "Add or scrape at least one knowledge base fact."),
]


def university_setup_status(university_id: str) -> Dict[str, Any]:
    """Derived fresh from real data every call, never a stored flag --
    mirrors accounts.serializers.student_onboarding_status."""
    from django_api.models import UniversityKnowledgeEntry

    university = University.objects.filter(uuid=university_id).first()

    if university is None:
        return {
            "profile_exists": False,
            "has_description": False,
            "has_contacts": False,
            "has_eligibility_criteria": False,
            "has_scrape_urls": False,
            "has_knowledge_facts": False,
            "setup_complete": False,
            "completion_percentage": 0,
            "missing_steps": [text for _, text in _COMPLETION_STEPS],
        }

    has_contacts = bool(
        university.contact_email
        or university.contact_phone
        or university.website_url
        or university.admissions_office_address
    )
    has_eligibility_criteria = bool(university.eligibility_criteria)
    has_scrape_urls = bool(university.scrape_urls)
    has_knowledge_facts = UniversityKnowledgeEntry.objects.filter(university_id=university_id).exists()
    has_description = bool(university.description)

    flags = {
        "has_description": has_description,
        "has_contacts": has_contacts,
        "has_eligibility_criteria": has_eligibility_criteria,
        "has_scrape_urls": has_scrape_urls,
        "has_knowledge_facts": has_knowledge_facts,
    }

    completed_count = sum(1 for flag_name, _ in _COMPLETION_STEPS if flags[flag_name])
    completion_percentage = round((completed_count / len(_COMPLETION_STEPS)) * 100)
    missing_steps = [text for flag_name, text in _COMPLETION_STEPS if not flags[flag_name]]

    return {
        "profile_exists": True,
        **flags,
       
        "setup_complete": (
            has_description and has_contacts and has_eligibility_criteria and has_knowledge_facts
        ),
        "completion_percentage": completion_percentage,
        "missing_steps": missing_steps,
    }
