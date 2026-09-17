from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Iterable
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from url_discovery.models import DiscoveredUrl, DiscoveryJob


# BASE IMP URLS now represents a navigation hierarchy, not a fixed-size ranking.
# It includes the homepage, top-level menu tabs, dropdown/mega-menu links, nested
# dropdown links and useful descendants discovered from those navigation pages.
CATEGORY_ORDER = [
    "Admissions & Application",
    "International Students & Visa",
    "Programs & Academics",
    "Fees, Tuition & Financial Aid",
    "Deadlines & Academic Calendar",
    "Housing, Dining & Campus Life",
    "Student Support Services",
    "Careers & Internships",
    "Forms, Documents & Downloads",
    "Contact & Help",
]

NAVIGATION_TERMS = {
    "about", "about us", "academics", "academic", "schools", "colleges",
    "departments", "programs", "degrees", "majors", "undergraduate", "graduate",
    "graduate programs", "admissions", "admissions aid", "admission", "apply",
    "application", "tuition", "aid", "tuition aid", "financial aid", "scholarships",
    "international", "international students", "research", "health medicine", "health",
    "medicine", "athletics", "campus life", "student life", "life on campus",
    "life on the farm", "housing", "dining", "student services", "student support",
    "careers", "career services", "internships", "calendar", "academic calendar",
    "directories", "directory", "maps", "map", "library", "forms", "contact",
    "contact us", "visit", "request info", "information for", "applicants",
    "faculty staff", "families", "alumni", "online learning", "continuing education",
    "community", "our impact", "civics", "artificial intelligence", "cancer research",
    "pilot", "wings", "student portal", "portal",
}

IMPORTANT_CHILD_TERMS = {
    "requirements", "how to apply", "application process", "deadlines", "dates",
    "first year", "freshman", "transfer", "international", "visa", "undergraduate",
    "graduate", "doctoral", "masters", "schools", "colleges", "departments",
    "programs", "degrees", "majors", "minors", "courses", "catalog", "curriculum",
    "research areas", "labs", "tuition", "fees", "cost", "financial aid",
    "scholarships", "assistantships", "billing", "housing", "residence halls",
    "dining", "campus life", "student organizations", "clubs", "health services",
    "counseling", "disability", "accessibility", "library", "academic support",
    "career services", "internships", "co op", "student employment", "job fairs",
    "calendar", "orientation", "forms", "documents", "contact", "directory",
    "visit", "request information", "faculty", "staff", "research", "centers",
}

LOW_VALUE_SEGMENTS = {
    "news", "event", "events", "story", "stories", "blog", "blogs", "article",
    "articles", "press", "media", "archive", "archives", "announcement",
    "announcements", "spotlight", "gallery", "photo", "photos", "video", "videos",
    "profile", "profiles", "person", "people", "faculty-profile", "staff-profile",
    "podcast", "tag", "category", "author", "search", "feed", "rss",
}

GENERIC_OR_FOOTER_LABELS = {
    "read more", "learn more", "more", "see more", "see all", "view all",
    "click here", "details", "explore", "open", "next", "previous", "back",
    "skip to content", "skip navigation", "menu", "close", "search", "submit",
    "privacy", "privacy policy", "terms", "terms of use", "cookie policy",
    "copyright", "accessibility statement", "site map", "sitemap", "webmaster",
    "facebook", "instagram", "linkedin", "youtube", "x", "twitter", "tiktok",
    "share", "print", "subscribe", "alerts", "emergency information",
}

DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx",
}

DATE_PATH_PATTERN = re.compile(
    r"(?:^|/)(?:19|20)\d{2}(?:/|$)|(?:^|/)(?:0?[1-9]|1[0-2])(?:/|$)"
)
NAV_REASON_PATTERN = re.compile(r"navigation hierarchy level\s+(\d+)", re.I)
MAX_NAVIGATION_LEVEL = 6


def _clean(value: str | None) -> str:
    decoded = unquote(value or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", decoded).strip()


def _record_url(record: UrlRecord) -> str:
    return record.final_url or record.normalized_url


def _url_identity(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlsplit(url)
    path = re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/") or "/"
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path.lower()}"


def _path_parts(url: str) -> list[str]:
    return [part for part in urlsplit(url).path.lower().split("/") if part]


def _label(record: UrlRecord) -> str:
    return _clean(record.anchor_text or record.h1 or record.page_title)


def _navigation_level(record: UrlRecord) -> int | None:
    match = NAV_REASON_PATTERN.search(record.classification_reason or "")
    return int(match.group(1)) if match else None


def _is_same_university_domain(project: Project, url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    root = (project.root_domain or "").lower().rstrip(".")
    return bool(hostname and root and (hostname == root or hostname.endswith(f".{root}")))


def _has_phrase(text: str, phrases: set[str]) -> bool:
    padded = f" {text} "
    return any(f" {phrase} " in padded for phrase in phrases)


def _is_noise(record: UrlRecord) -> bool:
    url = _record_url(record)
    parts = _path_parts(url)
    label = _label(record)
    title = _clean(record.page_title)

    if any(part in LOW_VALUE_SEGMENTS for part in parts):
        return True
    if DATE_PATH_PATTERN.search("/" + "/".join(parts)):
        return True
    if label in GENERIC_OR_FOOTER_LABELS or title in GENERIC_OR_FOOTER_LABELS:
        return True
    if label and (len(label) > 120 or len(label.split()) > 16):
        return True
    if record.http_status is not None and record.http_status >= 400:
        return True
    if any(term in title for term in ("404", "page not found", "access denied", "error")):
        return True
    return False


def _is_descendant_url(parent_url: str, child_url: str) -> bool:
    parent = urlsplit(parent_url)
    child = urlsplit(child_url)
    if (parent.hostname or "").lower() != (child.hostname or "").lower():
        return False
    parent_path = (parent.path or "/").rstrip("/")
    child_path = (child.path or "/").rstrip("/")
    if parent_path in {"", "/"}:
        return child_path not in {"", "/"}
    return child_path.startswith(parent_path + "/")


def navigation_importance_score(project: Project, record: UrlRecord) -> float:
    """Rank a record as a menu, dropdown or useful navigation descendant."""
    url = _record_url(record)
    base_key = _url_identity(project.base_url)
    url_key = _url_identity(url)
    parent_key = _url_identity(record.parent_url)
    label = _label(record)
    parts = _path_parts(url)
    nav_level = _navigation_level(record)

    if url_key == base_key:
        return 1000.0

    score = float(record.relevance_score or 0.0) * 0.35
    if nav_level is not None:
        score += max(55.0, 165.0 - (nav_level * 18.0))
    if parent_key == base_key:
        score += 90.0

    depth = max(0, int(record.crawl_depth or 0))
    if depth == 1:
        score += 35.0
    elif depth == 2:
        score += 24.0
    elif depth == 3:
        score += 12.0
    elif depth <= 5:
        score += 4.0
    else:
        score -= float(depth - 5) * 6.0

    if len(parts) == 1:
        score += 30.0
    elif len(parts) == 2:
        score += 22.0
    elif len(parts) == 3:
        score += 12.0
    elif len(parts) > 6:
        score -= float(len(parts) - 6) * 7.0

    if _has_phrase(label, NAVIGATION_TERMS):
        score += 55.0
    if _has_phrase(label, IMPORTANT_CHILD_TERMS):
        score += 40.0
    if record.decision_status == "relevant":
        score += 28.0
    elif record.decision_status == "review":
        score += 15.0

    if label:
        words = len(label.split())
        if words <= 5:
            score += 14.0
        elif words <= 10:
            score += 6.0
    if record.http_status is not None and 200 <= record.http_status < 400:
        score += 5.0
    if urlsplit(url).query:
        score -= 10.0
    if (record.file_extension or "").lower() in DOCUMENT_EXTENSIONS:
        score -= 10.0
    if _is_noise(record):
        score -= 300.0
    return round(score, 2)


# Backward-compatible name used by API/tests from older patches.
base_importance_score = navigation_importance_score


def _deduplicate(project: Project, records: Iterable[UrlRecord]) -> list[UrlRecord]:
    best: dict[str, UrlRecord] = {}
    for record in records:
        url = _record_url(record)
        if not url or not _is_same_university_domain(project, url):
            continue
        key = _url_identity(url)
        current = best.get(key)
        if current is None or navigation_importance_score(project, record) > navigation_importance_score(project, current):
            best[key] = record
    return list(best.values())


def _is_navigation_candidate(project: Project, record: UrlRecord, direct_home: bool = False) -> bool:
    if _is_noise(record):
        return False
    if _navigation_level(record) is not None:
        return True

    label = _label(record)
    useful = record.decision_status in {"relevant", "review"}
    explicit = _has_phrase(label, NAVIGATION_TERMS | IMPORTANT_CHILD_TERMS)
    shallow = len(_path_parts(_record_url(record))) <= 4
    concise = bool(label) and len(label.split()) <= 10

    if direct_home:
        return explicit or useful or (shallow and concise)
    return useful and (explicit or concise)


def _fallback_main_records(project: Project, records: list[UrlRecord]) -> list[UrlRecord]:
    """Support records produced before navigation markers were introduced."""
    base_key = _url_identity(project.base_url)
    candidates = [
        record
        for record in records
        if _url_identity(record.parent_url) == base_key
        and _is_navigation_candidate(project, record, direct_home=True)
        and navigation_importance_score(project, record) >= 55.0
    ]
    return sorted(candidates, key=lambda item: (item.id, -navigation_importance_score(project, item)))


def select_base_important_urls(
    project: Project,
    records: Iterable[UrlRecord],
    limit: int | None = None,
) -> list[UrlRecord]:
    """Return all important menu, dropdown and nested navigation URLs.

    The selection includes:
    1. The submitted homepage.
    2. Top-level header/utility navigation links.
    3. Hidden dropdown and mega-menu links present in the page HTML.
    4. Nested dropdown links and useful child links from those destination pages.
    5. Important documents linked from that hierarchy when documents are enabled.

    There is no default count cap. The crawl's page budget and navigation depth
    are the safety boundaries; low-value news/profile/archive branches are removed.
    """
    unique_records = _deduplicate(project, records)
    if not unique_records:
        return []

    base_key = _url_identity(project.base_url)
    homepage = next(
        (record for record in unique_records if _url_identity(_record_url(record)) == base_key),
        None,
    )

    by_key = {_url_identity(_record_url(record)): record for record in unique_records}
    children_by_parent: dict[str, list[UrlRecord]] = defaultdict(list)
    for record in unique_records:
        parent_key = _url_identity(record.parent_url)
        if parent_key:
            children_by_parent[parent_key].append(record)

    for children in children_by_parent.values():
        children.sort(
            key=lambda item: (
                _navigation_level(item) is None,
                _navigation_level(item) if _navigation_level(item) is not None else 99,
                item.id,
                -navigation_importance_score(project, item),
            )
        )

    chosen: list[UrlRecord] = []
    chosen_keys: set[str] = set()
    expansion_queue: deque[tuple[str, int]] = deque()

    def add(record: UrlRecord | None, level: int = 0) -> bool:
        if record is None or _is_noise(record):
            return False
        key = _url_identity(_record_url(record))
        if not key or key in chosen_keys:
            return False
        chosen.append(record)
        chosen_keys.add(key)
        expansion_queue.append((key, level))
        return True

    add(homepage, 0)

    # New-crawl seed: all explicitly marked top-level menu links. Old-crawl
    # fallback: strong direct-home links inferred from parent/path/label.
    top_level = [
        record
        for record in unique_records
        if _navigation_level(record) == 1
        or (
            _url_identity(record.parent_url) == base_key
            and _is_navigation_candidate(project, record, direct_home=True)
        )
    ]
    if not top_level:
        top_level = _fallback_main_records(project, unique_records)
    top_level.sort(key=lambda item: (item.id, -navigation_importance_score(project, item)))
    for record in top_level:
        add(record, 1)

    # Follow the recorded logical parent graph. Dropdown links point to their
    # owning top-level item, and nested dropdown links point to the nearest parent
    # menu item, so this walks A -> A/a -> A/a/i without a fixed total limit.
    expanded: set[str] = set()
    while expansion_queue:
        parent_key, parent_level = expansion_queue.popleft()
        if parent_key in expanded or parent_level >= MAX_NAVIGATION_LEVEL:
            continue
        expanded.add(parent_key)
        parent_record = by_key.get(parent_key)

        for child in children_by_parent.get(parent_key, []):
            child_level = _navigation_level(child)
            effective_level = child_level if child_level is not None else parent_level + 1
            if effective_level > MAX_NAVIGATION_LEVEL:
                continue
            if not _is_navigation_candidate(project, child):
                continue
            add(child, effective_level)

        # URL ancestry fallback catches old mega menus where every dropdown link
        # was recorded as a homepage child instead of its visual parent.
        if parent_record is not None:
            parent_url = _record_url(parent_record)
            for child in unique_records:
                child_key = _url_identity(_record_url(child))
                if child_key in chosen_keys or _is_noise(child):
                    continue
                if not _is_descendant_url(parent_url, _record_url(child)):
                    continue
                child_level = _navigation_level(child)
                if child_level is None and child.decision_status not in {"relevant", "review"}:
                    continue
                if child_level is not None and child_level > MAX_NAVIGATION_LEVEL:
                    continue
                relative_parts = len(_path_parts(_record_url(child))) - len(_path_parts(parent_url))
                if relative_parts > 3 and child_level is None:
                    continue
                add(child, child_level or parent_level + 1)

    # Include marked menu records whose parent was cross-domain/subdomain or was
    # not crawlable. They are still genuine dropdown links captured from the DOM.
    orphans = [
        record
        for record in unique_records
        if _navigation_level(record) is not None
        and _url_identity(_record_url(record)) not in chosen_keys
        and not _is_noise(record)
    ]
    orphans.sort(
        key=lambda item: (
            _navigation_level(item) or 99,
            item.id,
            -navigation_importance_score(project, item),
        )
    )
    for record in orphans:
        add(record, _navigation_level(record) or 1)

    if limit is not None and int(limit) > 0:
        return chosen[: int(limit)]
    return chosen


# --- STUDENT ESSENTIAL URLS FILTER ---

STUDENT_ESSENTIAL_MIN_LIMIT = 30
STUDENT_ESSENTIAL_MAX_LIMIT = 50

# Each entry contains:
# topic name, universal matching terms, preferred maximum per topic.
STUDENT_ESSENTIAL_RULES = [
    (
        "Admissions & Application",
        {
            "admissions", "admission", "apply", "application",
            "how to apply", "application process", "admission requirements",
            "first year", "freshman", "transfer admission",
            "undergraduate admission", "graduate admission",
            "admitted students",
        },
        5,
    ),
    (
        "Programs & Degrees",
        {
            "academics", "academic programs", "programs", "degrees",
            "degree programs", "majors", "minors", "departments",
            "schools", "colleges", "course offerings",
        },
        5,
    ),
    (
        "Tuition & Financial Aid",
        {
            "tuition", "fees", "tuition fees", "cost of attendance",
            "financial aid", "scholarships", "assistantships",
            "student accounts", "billing", "payment information",
        },
        5,
    ),
    (
        "Deadlines & Academic Calendar",
        {
            "deadlines", "important dates", "academic calendar",
            "semester dates", "registration dates", "exam schedule",
            "examination schedule", "orientation dates", "calendar",
        },
        4,
    ),
    (
        "International Students",
        {
            "international students", "international admission",
            "international admissions", "visa", "i 20", "immigration",
            "english proficiency", "toefl", "ielts",
            "international orientation", "arrival support",
        },
        4,
    ),
    (
        "Course Catalog & Curriculum",
        {
            "catalog", "course catalog", "courses", "curriculum",
            "degree requirements", "academic policies",
            "academic catalog", "course descriptions",
        },
        4,
    ),
    (
        "Housing & Dining",
        {
            "housing", "residence life", "residence halls", "dorms",
            "on campus housing", "off campus housing", "dining",
            "meal plans", "campus dining",
        },
        4,
    ),
    (
        "Student Support",
        {
            "student support", "student services", "academic advising",
            "advising", "tutoring", "academic support", "counseling",
            "counselling", "health services", "student health",
            "disability support", "accessibility",
        },
        5,
    ),
    (
        "Careers & Internships",
        {
            "career services", "careers", "internships", "internship",
            "co op", "student employment", "job fairs",
            "career counseling", "career centre", "career center",
        },
        4,
    ),
    (
        "Library & Learning Resources",
        {
            "library", "libraries", "learning center", "learning centre",
            "writing center", "writing centre", "study resources",
            "research databases",
        },
        3,
    ),
    (
        "Campus Life",
        {
            "campus life", "student life", "student activities",
            "student organizations", "student organisations",
            "clubs", "recreation", "campus recreation",
        },
        4,
    ),
    (
        "Safety, Maps & Transportation",
        {
            "campus safety", "public safety", "campus police",
            "emergency", "transportation", "campus transportation",
            "shuttle", "parking", "campus map", "maps",
        },
        4,
    ),
    (
        "Forms & Documents",
        {
            "forms", "student forms", "downloads", "documents",
            "checklists", "brochures", "guides",
            "application forms", "financial aid forms",
        },
        3,
    ),
    (
        "Contact & Directory",
        {
            "contact", "contact us", "directory", "campus directory",
            "office directory", "departments directory",
            "admissions contact", "student services contact",
        },
        3,
    ),
    (
        "Student Portals & Systems",
        {
            "student portal", "portal", "learning management system",
            "canvas", "blackboard", "moodle", "banner",
            "student email", "class registration",
        },
        3,
    ),
    (
        "Registrar & Academic Records",
        {
            "registrar", "academic records", "transcripts",
            "course registration", "registration", "graduation",
            "commencement", "student records",
        },
        4,
    ),
]

STUDENT_ESSENTIAL_CAPS = {
    topic: cap
    for topic, _terms, cap in STUDENT_ESSENTIAL_RULES
}

STUDENT_BASE_LABELS = {
    "admissions", "apply", "academics", "programs", "degrees",
    "tuition", "financial aid", "scholarships", "academic calendar",
    "international students", "course catalog", "academic catalog",
    "housing", "dining", "student services", "student support",
    "career services", "library", "campus life", "student life",
    "campus safety", "transportation", "parking", "maps",
    "forms", "contact", "directory", "student portal", "registrar",
}

STUDENT_NOISE_EXCEPTIONS = {
    "events",   # Some universities place their academic calendar here.
    "people",   # Some universities place their directory here.
}


def _student_essential_text(record: UrlRecord) -> str:
    url = _record_url(record)

    return _clean(
        " ".join(
            filter(
                None,
                [
                    urlsplit(url).path,
                    record.anchor_text,
                    record.page_title,
                    record.h1,
                    record.meta_description,
                    record.primary_category,
                    record.parent_url,
                ],
            )
        )
    )


def _student_essential_topics(record: UrlRecord) -> list[str]:
    text = _student_essential_text(record)

    return [
        topic
        for topic, terms, _cap in STUDENT_ESSENTIAL_RULES
        if _has_phrase(text, terms)
    ]


def _student_family_key(record: UrlRecord) -> str:
    url = _record_url(record)
    parsed = urlsplit(url)
    parts = _path_parts(url)

    first_section = parts[0] if parts else "/"
    return f"{(parsed.hostname or '').lower()}/{first_section}"


def student_essential_score(project: Project, record: UrlRecord) -> float:
    """Score broad student-information hub pages across different universities."""

    url = _record_url(record)

    if not url or not _is_same_university_domain(project, url):
        return -10000.0

    if record.http_status is not None and record.http_status >= 400:
        return -10000.0

    base_key = _url_identity(project.base_url)
    url_key = _url_identity(url)
    is_homepage = url_key == base_key

    topics = _student_essential_topics(record)

    if not is_homepage and not topics:
        return -10000.0

    parts = _path_parts(url)

    # Keep calendar/directory hubs even if the existing generic noise rules
    # consider "events" or "people" low-value.
    if _is_noise(record):
        allowed_hub = (
            len(parts) <= 2
            and bool(parts)
            and parts[0] in STUDENT_NOISE_EXCEPTIONS
            and bool(topics)
        )

        if not allowed_hub:
            return -10000.0

    if is_homepage:
        return 1000.0

    label = _label(record)
    path_text = _clean(" ".join(parts))

    score = float(record.relevance_score or 0.0) * 0.35

    # Prefer broad parent/base pages.
    if len(parts) == 1:
        score += 55.0
    elif len(parts) == 2:
        score += 38.0
    elif len(parts) == 3:
        score += 20.0
    elif len(parts) == 4:
        score += 8.0
    else:
        score -= float(len(parts) - 4) * 10.0

    crawl_depth = max(0, int(record.crawl_depth or 0))

    if crawl_depth == 1:
        score += 30.0
    elif crawl_depth == 2:
        score += 18.0
    elif crawl_depth == 3:
        score += 8.0
    elif crawl_depth > 4:
        score -= float(crawl_depth - 4) * 5.0

    if _url_identity(record.parent_url) == base_key:
        score += 32.0

    if record.decision_status == "relevant":
        score += 20.0
    elif record.decision_status == "review":
        score += 10.0

    if record.is_discovery_only:
        score -= 12.0

    if record.http_status is not None and 200 <= record.http_status < 400:
        score += 5.0

    if urlsplit(url).query:
        score -= 12.0

    if (record.file_extension or "").lower() in DOCUMENT_EXTENSIONS:
        # Documents may still be useful, but base web pages are preferred.
        score -= 15.0

    if label:
        word_count = len(label.split())

        if word_count <= 5:
            score += 16.0
        elif word_count > 12:
            score -= 12.0

        if label in STUDENT_BASE_LABELS:
            score += 38.0
        elif _has_phrase(label, STUDENT_BASE_LABELS):
            score += 22.0

    if path_text in STUDENT_BASE_LABELS:
        score += 30.0

    # More topic coverage can indicate a broad student hub page.
    score += min(18.0, float(len(topics) - 1) * 6.0)

    return round(score, 2)


def select_student_essential_urls(
    project: Project,
    records: Iterable[UrlRecord],
    limit: int | None = None,
) -> list[UrlRecord]:
    """Return approximately 30–50 universal student-information base pages."""

    requested_limit = int(limit or STUDENT_ESSENTIAL_MAX_LIMIT)
    max_limit = max(
        STUDENT_ESSENTIAL_MIN_LIMIT,
        min(STUDENT_ESSENTIAL_MAX_LIMIT, requested_limit),
    )
    minimum_target = min(STUDENT_ESSENTIAL_MIN_LIMIT, max_limit)

    unique_records = _deduplicate(project, records)

    ranked_candidates: list[tuple[float, list[str], UrlRecord]] = []

    for record in unique_records:
        score = student_essential_score(project, record)

        if score <= -1000:
            continue

        topics = _student_essential_topics(record)
        is_homepage = _url_identity(_record_url(record)) == _url_identity(project.base_url)

        if is_homepage or topics:
            ranked_candidates.append((score, topics, record))

    ranked_candidates.sort(
        key=lambda item: (
            -item[0],
            len(_path_parts(_record_url(item[2]))),
            item[2].id,
        )
    )

    selected: list[UrlRecord] = []
    selected_keys: set[str] = set()
    family_counts: dict[str, int] = defaultdict(int)
    topic_counts: dict[str, int] = defaultdict(int)

    def add_candidate(
        candidate: tuple[float, list[str], UrlRecord],
        family_limit: int,
        enforce_topic_caps: bool,
    ) -> bool:
        _score, topics, record = candidate
        key = _url_identity(_record_url(record))

        if not key or key in selected_keys:
            return False

        family = _student_family_key(record)
        is_homepage = key == _url_identity(project.base_url)

        if not is_homepage and family_counts[family] >= family_limit:
            return False

        if (
            enforce_topic_caps
            and topics
            and all(
                topic_counts[topic] >= STUDENT_ESSENTIAL_CAPS.get(topic, 3)
                for topic in topics
            )
        ):
            return False

        selected.append(record)
        selected_keys.add(key)

        if not is_homepage:
            family_counts[family] += 1

        for topic in topics:
            topic_counts[topic] += 1

        return True

    # Always add the university homepage when it exists in the crawl.
    for candidate in ranked_candidates:
        if _url_identity(_record_url(candidate[2])) == _url_identity(project.base_url):
            add_candidate(candidate, family_limit=99, enforce_topic_caps=False)
            break

    topic_order = [
        topic
        for topic, _terms, _cap in STUDENT_ESSENTIAL_RULES
    ]

    topic_buckets: dict[str, list[tuple[float, list[str], UrlRecord]]] = {
        topic: [
            candidate
            for candidate in ranked_candidates
            if topic in candidate[1]
        ]
        for topic in topic_order
    }

    # First pass: obtain at least one strong page for every available topic.
    for topic in topic_order:
        for candidate in topic_buckets[topic]:
            if add_candidate(
                candidate,
                family_limit=4,
                enforce_topic_caps=False,
            ):
                break

    # Round-robin pass: expand topic coverage until approximately 30 records.
    while len(selected) < minimum_target:
        progress = False

        for topic in topic_order:
            if len(selected) >= minimum_target:
                break

            if topic_counts[topic] >= STUDENT_ESSENTIAL_CAPS[topic]:
                continue

            for candidate in topic_buckets[topic]:
                if add_candidate(
                    candidate,
                    family_limit=4,
                    enforce_topic_caps=False,
                ):
                    progress = True
                    break

        if not progress:
            break

    # Fill remaining places with the strongest broad student pages.
    for candidate in ranked_candidates:
        if len(selected) >= max_limit:
            break

        add_candidate(
            candidate,
            family_limit=4,
            enforce_topic_caps=True,
        )

    # If a small university has fewer diversified families, relax only enough
    # to approach the requested minimum. Irrelevant URLs are still never added.
    if len(selected) < minimum_target:
        for candidate in ranked_candidates:
            if len(selected) >= minimum_target:
                break

            add_candidate(
                candidate,
                family_limit=8,
                enforce_topic_caps=False,
            )

    return selected[:max_limit]

