"""Presentation categories for the canonical university knowledge records."""
import hashlib
import json
import re

CATEGORIES = (
    ("scholarships", "Scholarships & financial aid", r"scholarship|financial aid|fellowship|assistantship|stipend|grant\b"),
    ("fees", "Tuition & costs", r"tuition|\bfees?\b|cost|refund|payment"),
    ("admissions", "Admissions & eligibility", r"admission|eligibility|application|deadline|\bgpa\b|\bgre\b|\bgmat\b|ielts|toefl|requirement"),
    ("academics", "Programs & academics", r"program|course|degree|curriculum|academic|faculty|research"),
    ("international", "International students", r"international|visa|immigration|i-20|\bcpt\b|\bopt\b"),
    ("campus", "Campus & student life", r"campus|housing|accommodation|dining|student life|health|safety|athletic"),
    ("careers", "Careers & outcomes", r"career|placement|employment|internship|alumni"),
    ("overview", "University overview & contacts", r"overview|about|contact|address|location|description|website"),
    ("other", "Other", None),
)
CATEGORY_IDS = {item[0] for item in CATEGORIES}
OVERVIEW_FIELDS = ('name', 'location', 'admissions_office_address', 'website_url',
                   'contact_email', 'contact_phone', 'description')


def category_for(entry):
    explicit = (entry.details or {}).get("_information_category")
    if explicit in CATEGORY_IDS:
        return explicit
    # Titles are a better signal than incidental words in a long scraped page.
    for text in (entry.topic, entry.content):
        for slug, _, pattern in CATEGORIES:
            if pattern and re.search(pattern, text, re.I):
                return slug
    return "other"


def revision_for(entry):
    payload = [entry.topic, entry.content, entry.details, entry.source_type, entry.group_id, entry.confidence]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


RESEARCH_FIELDS = {
    "course": ("name", "level", "duration", "study_mode", "tuition", "currency", "seats", "academic_year", "requirements"),
    "intake": ("course_name", "term", "year", "deadline", "applicant_scope"),
}


def research_models():
    from university_research.models import UniversityFact, UniversityCourse, UniversityIntake, UniversityPage
    return {"fact": UniversityFact, "course": UniversityCourse, "intake": UniversityIntake, "page": UniversityPage}


def research_key(kind, url, identity):
    return hashlib.sha256(json.dumps([kind, url, identity], sort_keys=True).encode()).hexdigest()


def research_record(kind, row):
    from types import SimpleNamespace
    url = row.url if kind == "page" else row.page.url
    if kind == "fact":
        topic, content, details, identity = row.topic, row.content, {}, row.topic
    elif kind == "page":
        topic, content, details, identity = row.title or "Official website page", row.content, {}, url
    else:
        details = {field: getattr(row, field) for field in RESEARCH_FIELDS[kind]}
        topic = row.name if kind == "course" else f"{row.course_name} {row.term} intake".strip()
        identity = row.name if kind == "course" else [row.course_name, row.term, row.year]
        content = row.source_quote or "\n".join(f"{key.replace('_', ' ')}: {value}" for key, value in details.items() if value)
    key = research_key(kind, url, identity)
    revision = hashlib.sha256(json.dumps([topic, content, details], sort_keys=True).encode()).hexdigest()
    provider = row.provider if kind == "page" else row.page.provider
    return {"id": f"research:{kind}:{row.pk}", "topic": topic[:500], "content": content,
            "details": details, "source_type": "scraped" if provider in {"scraper", "scraper_extracted"} else "research", "source_url": url,
            "category": "academics" if kind == "course" else "admissions" if kind == "intake" else category_for(SimpleNamespace(topic=topic, content=content, details={})),
            "revision": revision, "research_key": key, "record_kind": kind}


def overlay_research(row, evidence):
    """Overlay durable officer corrections on newly scraped research, including after row IDs change."""
    if not row.registered_university_id:
        return evidence
    from django_api.models import UniversityKnowledgeEntry
    overrides = list(UniversityKnowledgeEntry.objects.filter(
        university_id=str(row.registered_university.uuid), source_type="human_verified",
        details__has_key="_information_research_key",
    ).defer("embedding"))
    by_key = {(entry.details or {})["_information_research_key"]: entry for entry in overrides}
    page_urls = {entry.source_url for entry in overrides if entry.details.get("_information_research_kind") == "page"}
    for section, kind in (("facts", "fact"), ("courses", "course"), ("intakes", "intake")):
        result = []
        for item in evidence.get(section, []):
            url = item.get("source_url") or item.get("page__url")
            if url in page_urls:
                continue
            identity = item.get("topic") if kind == "fact" else item.get("name") if kind == "course" else [item.get("course_name"), item.get("term"), item.get("year")]
            override = by_key.get(research_key(kind, url, identity))
            if override:
                details = {k: v for k, v in override.details.items() if not k.startswith("_information_")}
                item = {**item, **details, "source_type": "human_verified", "source_quote": override.content}
                if kind == "fact":
                    item.update(topic=override.topic, content=override.content)
            result.append(item)
        evidence[section] = result
    # Include edits even if their new vocabulary no longer matches the stale
    # scraped index, or a refreshed scrape no longer contains the original row.
    evidence["saved_knowledge"] = [{"topic": entry.topic, "content": entry.content,
        "details": {k: v for k, v in entry.details.items() if not k.startswith("_information_")},
        "source_type": "human_verified", "source_url": entry.source_url} for entry in overrides]
    evidence["instruction"] = "Current university-verified corrections in saved_knowledge supersede older website records and previous answers on the same topic."
    return evidence
