from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
import time
from collections import deque
from datetime import datetime, timezone
from html import unescape
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
from django.db import IntegrityError
from django.db.models import Count, F

from url_discovery.classifier import classify_url, crawl_priority
from url_discovery.domain_policy import DomainPolicy
from url_discovery.models import DiscoveredUrl, DiscoveryJob
from url_discovery.url_filter import hard_filter_url, supported_document
from url_discovery.url_normalizer import extension_from_url, normalize_url

LOGGER = logging.getLogger(__name__)

COMMON_PATHS = (
    "/admissions/", "/apply/", "/academics/", "/programs/", "/degrees/",
    "/international/", "/international-students/", "/tuition/", "/financial-aid/",
    "/scholarships/", "/housing/", "/student-life/", "/student-services/",
    "/career/", "/careers/", "/internships/", "/calendar/", "/academic-calendar/",
    "/forms/", "/contact/", "/catalog/", "/departments/", "/faculty/",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_text(value: str, limit: int = 7000) -> str:
    return " ".join(unescape(value or "").split())[:limit]


NAV_CONTEXT_TERMS = (
    "nav", "navigation", "menu", "dropdown", "submenu", "mega", "primary",
    "secondary", "utility", "audience", "global", "header", "masthead",
    "flyout", "offcanvas", "drawer",
)
DROPDOWN_CONTEXT_TERMS = (
    "dropdown", "submenu", "sub-menu", "mega", "flyout", "children",
    "menu-panel", "menu-group", "nav-group",
)
IMPORTANT_NAV_CHILD_TERMS = (
    "admission", "apply", "application", "requirement", "deadline", "transfer",
    "international", "visa", "program", "degree", "major", "minor", "school",
    "college", "department", "course", "catalog", "academic", "research", "lab",
    "tuition", "fee", "cost", "aid", "scholarship", "assistantship", "billing",
    "housing", "residence", "dining", "campus life", "student life", "health",
    "counseling", "disability", "accessibility", "library", "support", "career",
    "internship", "employment", "calendar", "orientation", "form", "document",
    "contact", "directory", "map", "visit", "request info", "about",
)
GENERIC_NAV_LABELS = {
    "", "read more", "learn more", "more", "see more", "see all", "view all",
    "click here", "details", "open", "next", "previous", "back", "menu", "close",
    "search", "submit", "skip to content", "skip navigation",
}
NAV_REASON_PATTERN = re.compile(r"navigation hierarchy level\s+(\d+)", re.I)


def _node_marker(node) -> str:
    attrs = getattr(node, "attrs", {}) or {}
    classes = attrs.get("class", [])
    if not isinstance(classes, list):
        classes = [str(classes)]
    return " ".join(
        [
            str(attrs.get("id", "")),
            " ".join(str(item) for item in classes),
            str(attrs.get("role", "")),
            str(attrs.get("aria-label", "")),
            str(attrs.get("data-menu", "")),
        ]
    ).lower()


def _is_navigation_anchor(link) -> bool:
    """Return True for header, menu, dropdown, mega-menu and local-nav links."""
    for node in [link, *list(link.parents)[:10]]:
        name = (getattr(node, "name", "") or "").lower()
        if name == "footer":
            return False
        if name in {"nav", "header"}:
            return True
        if any(term in _node_marker(node) for term in NAV_CONTEXT_TERMS):
            return True
    return False


def _is_global_navigation_anchor(link) -> bool:
    """Identify the university-wide header navigation repeated on inner pages."""
    for node in [link, *list(link.parents)[:10]]:
        name = (getattr(node, "name", "") or "").lower()
        marker = _node_marker(node)
        if name == "footer":
            return False
        if name == "header":
            return True
        if any(term in marker for term in ("global", "masthead", "site-header", "main-header", "primary-nav", "primary-menu")):
            return True
    return False


def _navigation_dom_level(link) -> int:
    """Estimate an anchor's visual nesting inside a menu/dropdown."""
    list_depth = 0
    dropdown_depth = 0
    for node in list(link.parents)[:12]:
        name = (getattr(node, "name", "") or "").lower()
        if name == "footer":
            return 0
        if name in {"ul", "ol"}:
            list_depth += 1
        marker = _node_marker(node)
        if any(term in marker for term in DROPDOWN_CONTEXT_TERMS):
            dropdown_depth += 1
        if name in {"nav", "header"}:
            break
    return max(1, min(5, max(list_depth, dropdown_depth + 1)))


def _first_parent_menu_anchor(link, resolve_base: str) -> str | None:
    """Find the clickable parent item for nested UL/LI menu markup."""
    for nested_list in link.find_parents(["ul", "ol"], limit=8):
        parent_item = nested_list.find_parent("li")
        if not parent_item:
            continue
        for child in parent_item.children:
            if not isinstance(child, Tag):
                continue
            if child is nested_list or child.name in {"ul", "ol"}:
                break
            candidate = child if child.name == "a" else child.find("a", href=True)
            if candidate and candidate is not link and candidate.get("href"):
                return urljoin(resolve_base, str(candidate.get("href")))
    return None


def _menu_controller_map(soup: BeautifulSoup, resolve_base: str) -> dict[str, str]:
    """Map aria/data controlled dropdown containers to their main menu URL."""
    mapping: dict[str, str] = {}
    control_attrs = ("aria-controls", "data-target", "data-bs-target", "data-controls")
    for trigger in soup.find_all(True):
        target_ids: list[str] = []
        for attr in control_attrs:
            raw = str(trigger.get(attr, "") or "").strip()
            if raw:
                target_ids.extend(part.lstrip("#") for part in re.split(r"[\s,]+", raw) if part)
        if not target_ids:
            continue

        href = str(trigger.get("href", "") or "").strip()
        controller_url = ""
        if href and not href.startswith("#"):
            controller_url = urljoin(resolve_base, href)
        if not controller_url:
            nearby = trigger.find_parent("a", href=True)
            if not nearby:
                parent = trigger.parent if isinstance(trigger.parent, Tag) else None
                if parent:
                    nearby = parent.find("a", href=True, recursive=False)
            if nearby and nearby.get("href") and not str(nearby.get("href")).startswith("#"):
                controller_url = urljoin(resolve_base, str(nearby.get("href")))
        if not controller_url:
            continue
        for target_id in target_ids:
            mapping[target_id] = controller_url
    return mapping


def _logical_navigation_parent(
    link,
    soup: BeautifulSoup,
    resolve_base: str,
    page_url: str,
    controller_map: dict[str, str],
) -> str:
    """Return the menu item that logically owns a dropdown/nested link."""
    nested_parent = _first_parent_menu_anchor(link, resolve_base)
    if nested_parent:
        return nested_parent

    for ancestor in list(link.parents)[:12]:
        if not isinstance(ancestor, Tag):
            continue
        ancestor_id = str(ancestor.get("id", "") or "").strip()
        if ancestor_id and ancestor_id in controller_map:
            return controller_map[ancestor_id]

        marker = _node_marker(ancestor)
        if any(term in marker for term in DROPDOWN_CONTEXT_TERMS):
            # Mega-menu columns often place their heading link before the child list.
            for candidate in ancestor.find_all("a", href=True, limit=8):
                if candidate is link:
                    break
                href = str(candidate.get("href", "") or "").strip()
                label = _clean_text(candidate.get_text(" ") or candidate.get("aria-label", ""), 200).lower()
                if href and not href.startswith("#") and label not in GENERIC_NAV_LABELS:
                    return urljoin(resolve_base, href)
    return page_url


def _structured_navigation_links(soup: BeautifulSoup, resolve_base: str) -> list[tuple[str, str]]:
    """Extract SiteNavigationElement/ItemList URLs from JSON-LD markup."""
    found: list[tuple[str, str]] = []

    def visit(value, inherited_navigation: bool = False, inherited_label: str = "") -> None:
        if isinstance(value, list):
            for item in value:
                visit(item, inherited_navigation, inherited_label)
            return
        if not isinstance(value, dict):
            return

        raw_type = value.get("@type", "")
        type_values = raw_type if isinstance(raw_type, list) else [raw_type]
        type_text = " ".join(str(item) for item in type_values).lower()
        own_label = str(value.get("name") or value.get("headline") or inherited_label or "").strip()
        navigation_context = inherited_navigation or "sitenavigationelement" in type_text
        if "itemlist" in type_text and any(term in own_label.lower() for term in ("nav", "menu", "links")):
            navigation_context = True

        if navigation_context:
            raw_url = value.get("url") or value.get("@id")
            if isinstance(raw_url, str) and raw_url.strip():
                found.append((urljoin(resolve_base, raw_url.strip()), own_label))

        for key, child in value.items():
            if key in {"itemListElement", "hasPart", "item", "children", "subOrganization"}:
                visit(child, navigation_context, own_label)
            elif navigation_context and isinstance(child, (dict, list)):
                visit(child, True, own_label)

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text(" ")
        if not raw.strip():
            continue
        try:
            visit(json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return found


def _onclick_url(value: str) -> str | None:
    match = re.search(
        r"(?:window\.)?location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]|"
        r"(?:open|navigate)\s*\(\s*['\"]([^'\"]+)['\"]",
        value or "",
        flags=re.I,
    )
    if not match:
        return None
    return match.group(1) or match.group(2)


def _important_navigation_hint(url: str, label: str) -> bool:
    cleaned = re.sub(r"[^a-z0-9]+", " ", f"{label} {url}".lower()).strip()
    if not cleaned or _clean_text(label, 200).lower() in GENERIC_NAV_LABELS:
        return False
    padded = f" {cleaned} "
    return any(f" {term} " in padded for term in IMPORTANT_NAV_CHILD_TERMS)


def _navigation_level_from_reason(reason: str | None) -> int | None:
    match = NAV_REASON_PATTERN.search(reason or "")
    return int(match.group(1)) if match else None


def _merge_navigation_reason(reason: str | None, level: int) -> str:
    marker = f"Navigation hierarchy level {level}"
    existing = (reason or "").strip()
    if NAV_REASON_PATTERN.search(existing):
        existing = NAV_REASON_PATTERN.sub(marker, existing)
        return existing[:4000]
    return f"{marker}; {existing}".rstrip("; ")[:4000]


class DirectUniversityCrawler:
    """In-process crawler that discovers a university's official pages from
    a single base URL, so an officer doesn't have to hand-type every scrape
    URL. URLs are persisted as soon as they are discovered, so results are
    visible even if some pages fail to download. Ported from the standalone
    auto_university_urls_parser prototype onto the Django ORM."""

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        job = DiscoveryJob.objects.select_related("university").get(id=job_id)
        self.base_url = job.base_url
        self.allowed_domains = job.allowed_domains or []
        self.config = job.settings or {}

        self.max_pages = int(self.config.get("max_pages", 150))
        self.max_depth = int(self.config.get("max_depth", 6))
        configured_navigation_depth = int(self.config.get("navigation_max_depth", max(4, self.max_depth)))
        self.navigation_max_depth = max(3, min(6, configured_navigation_depth))
        self.delay = max(0.0, float(self.config.get("download_delay", 0.25)))
        self.timeout = max(5, int(self.config.get("request_timeout", 20)))
        self.include_documents = bool(self.config.get("include_documents", True))
        self.respect_robots = bool(self.config.get("respect_robots_txt", True))
        self.relevant_threshold = float(self.config.get("relevant_threshold", 30))
        self.review_threshold = float(self.config.get("review_threshold", 1))
        self.domain_policy = DomainPolicy(
            self.base_url,
            self.allowed_domains,
            bool(self.config.get("include_subdomains", True)),
        )
        self.seen: set[str] = set()
        self.queued: set[str] = set()
        self.fetched: set[str] = set()
        self.queue: deque[tuple[str, str | None, str, int, int | None]] = deque()
        self.robot_parser: robotparser.RobotFileParser | None = None
        self.user_agent = "Mozilla/5.0 (compatible; KormicUniversityURLDiscovery/1.0; +educational-indexer)"

    def run(self) -> None:
        self._mark_running()
        try:
            self._prepare_robots()
            self._seed()
            with httpx.Client(
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.5",
                    "Accept-Language": "en-US,en;q=0.8",
                },
                follow_redirects=True,
                timeout=httpx.Timeout(self.timeout),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            ) as client:
                while self.queue:
                    job_status = self._job_status()
                    if job_status in {"stop_requested", "stopped"}:
                        self._finish("stopped")
                        return
                    if self._pages_crawled() >= self.max_pages:
                        break

                    url, parent_url, anchor_text, depth, navigation_level = self.queue.popleft()
                    self.queued.discard(url)
                    inside_normal_depth = depth <= self.max_depth
                    inside_navigation_depth = (
                        navigation_level is not None
                        and navigation_level <= self.navigation_max_depth
                    )
                    if not inside_normal_depth and not inside_navigation_depth:
                        continue
                    if not self._robots_allowed(url):
                        self._mark_fetch_failure(url, "Blocked by robots.txt", excluded=False)
                        self.fetched.add(url)
                        continue
                    self._fetch_and_parse(
                        client, url, parent_url, anchor_text, depth, navigation_level
                    )
                    self.fetched.add(url)
                    if self.delay:
                        time.sleep(self.delay)
            self._finish("completed")
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Discovery job %s failed", self.job_id)
            self._finish("failed", str(exc)[:4000])

    def _seed(self) -> None:
        # Queue discovery helpers first, then place the submitted homepage at the
        # front. This guarantees that even a small max-pages run inspects the
        # real homepage navigation before generic guessed paths.
        self._discover(
            urljoin(self.base_url, "/sitemap.xml"),
            self.base_url,
            "Sitemap",
            0,
            force_queue=True,
        )
        self._discover(
            urljoin(self.base_url, "/sitemap_index.xml"),
            self.base_url,
            "Sitemap index",
            0,
            force_queue=True,
        )
        for path in COMMON_PATHS:
            self._discover(
                urljoin(self.base_url, path),
                self.base_url,
                path.strip("/ ").replace("-", " "),
                1,
                force_queue=True,
            )
        self._discover(
            self.base_url,
            None,
            "University homepage",
            0,
            force_queue=True,
            priority_queue=True,
            navigation_level=0,
        )

    def _prepare_robots(self) -> None:
        if not self.respect_robots:
            return
        robots_url = urljoin(self.base_url, "/robots.txt")
        parser = robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            parser.read()
            self.robot_parser = parser
            for sitemap in parser.site_maps() or []:
                self._discover(sitemap, robots_url, "Sitemap from robots.txt", 0, force_queue=True)
        except Exception as exc:  # network/SSL/parser issues must not kill crawling
            LOGGER.info("robots.txt unavailable for %s: %s", self.base_url, exc)
            self.robot_parser = None

    def _robots_allowed(self, url: str) -> bool:
        if not self.robot_parser:
            return True
        try:
            return self.robot_parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def _enqueue(
        self,
        normalized: str,
        parent_url: str | None,
        anchor_text: str,
        depth: int,
        navigation_level: int | None,
        priority: bool,
    ) -> None:
        if normalized in self.queued or normalized in self.fetched:
            return
        item = (normalized, parent_url, anchor_text, depth, navigation_level)
        if priority:
            self.queue.appendleft(item)
        else:
            self.queue.append(item)
        self.queued.add(normalized)

    def _upgrade_existing_discovery(
        self,
        normalized: str,
        parent_url: str | None,
        anchor_text: str,
        depth: int,
        navigation_level: int | None,
        force_queue: bool,
        priority_queue: bool,
    ) -> None:
        """Upgrade a sitemap/body discovery when it later appears in a menu."""
        record = DiscoveredUrl.objects.filter(job_id=self.job_id, normalized_url=normalized).first()
        if record:
            changed_fields: list[str] = []
            if anchor_text and (
                not record.anchor_text
                or len(anchor_text.strip()) < len((record.anchor_text or "").strip())
                or navigation_level is not None
            ):
                record.anchor_text = anchor_text[:500]
                changed_fields.append("anchor_text")
            if navigation_level is not None:
                existing_level = _navigation_level_from_reason(record.classification_reason)
                effective_level = (
                    navigation_level
                    if existing_level is None
                    else min(existing_level, navigation_level)
                )
                if existing_level is None or navigation_level < existing_level:
                    record.parent_url = parent_url or record.parent_url
                    record.crawl_depth = min(int(record.crawl_depth or depth), depth)
                    changed_fields.extend(["parent_url", "crawl_depth"])
                if record.decision_status in {"excluded", "pending"}:
                    record.decision_status = "review"
                record.is_discovery_only = False
                record.classification_reason = _merge_navigation_reason(
                    record.classification_reason, effective_level
                )
                changed_fields.extend(["decision_status", "is_discovery_only", "classification_reason"])
            if changed_fields:
                record.save(update_fields=list(dict.fromkeys(changed_fields)))

        inside_normal_depth = depth <= self.max_depth
        inside_navigation_depth = (
            navigation_level is not None
            and navigation_level <= self.navigation_max_depth
        )
        if (force_queue or inside_normal_depth or inside_navigation_depth) and (
            normalized not in self.fetched and normalized not in self.queued
        ):
            self._enqueue(
                normalized,
                parent_url,
                anchor_text,
                depth,
                navigation_level,
                priority_queue or navigation_level is not None,
            )

    def _discover(
        self,
        url: str,
        parent_url: str | None,
        anchor_text: str,
        depth: int,
        force_queue: bool = False,
        priority_queue: bool = False,
        navigation_level: int | None = None,
        inherit_navigation_level: int | None = None,
    ) -> None:
        normalized = normalize_url(url)
        if not normalized:
            return
        if not self.domain_policy.is_allowed(normalized):
            return
        allowed, _reason = hard_filter_url(normalized, self.include_documents)
        if not allowed:
            return

        classification = classify_url(
            url=normalized,
            anchor_text=anchor_text,
            parent_url=parent_url,
            file_extension=extension_from_url(normalized),
            relevant_threshold=self.relevant_threshold,
            review_threshold=self.review_threshold,
        )
        if (
            navigation_level is None
            and inherit_navigation_level is not None
            and classification.decision_status in {"relevant", "review"}
        ):
            navigation_level = inherit_navigation_level

        if normalized in self.seen:
            self._upgrade_existing_discovery(
                normalized,
                parent_url,
                anchor_text,
                depth,
                navigation_level,
                force_queue,
                priority_queue,
            )
            return

        self.seen.add(normalized)
        storage = classification.as_storage()
        if navigation_level is not None:
            if storage["decision_status"] in {"excluded", "pending"}:
                storage["decision_status"] = "review"
            storage["is_discovery_only"] = False
            storage["classification_reason"] = _merge_navigation_reason(
                storage.get("classification_reason"), navigation_level
            )

        try:
            DiscoveredUrl.objects.create(
                job_id=self.job_id,
                original_url=url,
                normalized_url=normalized,
                parent_url=parent_url,
                anchor_text=anchor_text[:500] if anchor_text else None,
                file_extension=extension_from_url(normalized) or None,
                crawl_depth=depth,
                **storage,
            )
        except IntegrityError:
            return

        DiscoveryJob.objects.filter(id=self.job_id).update(pages_discovered=F("pages_discovered") + 1)

        score = classification.relevance_score
        inside_normal_depth = depth <= self.max_depth
        inside_navigation_depth = (
            navigation_level is not None
            and navigation_level <= self.navigation_max_depth
        )
        should_queue = force_queue or inside_navigation_depth or (
            inside_normal_depth
            and (
                depth <= 2
                or score >= self.review_threshold
                or crawl_priority(normalized, anchor_text) > 0
            )
        )
        if should_queue:
            self._enqueue(
                normalized,
                parent_url,
                anchor_text,
                depth,
                navigation_level,
                priority_queue
                or navigation_level is not None
                or score >= self.relevant_threshold,
            )

    def _fetch_and_parse(
        self,
        client: httpx.Client,
        url: str,
        parent_url: str | None,
        anchor_text: str,
        depth: int,
        navigation_level: int | None,
    ) -> None:
        try:
            response = client.get(url)
        except httpx.TransportError as exc:
            if "certificate" in str(exc).lower() or "ssl" in str(exc).lower():
                try:
                    with httpx.Client(headers=client.headers, follow_redirects=True, timeout=self.timeout, verify=False) as insecure:
                        response = insecure.get(url)
                except Exception as retry_exc:  # noqa: BLE001
                    self._mark_fetch_failure(url, str(retry_exc))
                    return
            else:
                self._mark_fetch_failure(url, str(exc))
                return
        except Exception as exc:  # noqa: BLE001
            self._mark_fetch_failure(url, str(exc))
            return

        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
        final_url = normalize_url(str(response.url)) or url
        if response.status_code >= 400:
            self._mark_fetch_failure(url, f"HTTP {response.status_code}", http_status=response.status_code)
            return

        body = response.content
        is_sitemap = (
            "xml" in content_type
            or final_url.lower().endswith((".xml", ".xml.gz"))
            or b"<urlset" in body[:500].lower()
            or b"<sitemapindex" in body[:500].lower()
        )
        if is_sitemap:
            self._record_page(url, final_url, response.status_code, content_type, depth, "XML sitemap", "", "", "", anchor_text, parent_url, True)
            self._parse_sitemap(body, final_url, depth)
            return

        if supported_document(final_url) or content_type == "application/pdf":
            # Document text extraction is intentionally not done here -- this
            # crawler only proposes candidate scrape_urls. The existing
            # knowledge.scraper fact-extraction pipeline doesn't parse PDFs
            # either, so behaviour stays consistent either way.
            filename = final_url.rstrip("/").rsplit("/", 1)[-1]
            self._record_page(url, final_url, response.status_code, content_type, depth, filename, "", "", "", anchor_text, parent_url, False)
            return

        if "html" not in content_type and not body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            self._record_page(url, final_url, response.status_code, content_type, depth, "", "", "", "", anchor_text, parent_url, True)
            return

        soup = BeautifulSoup(response.text, "html.parser")
        base_tag = soup.find("base", href=True)
        resolve_base = urljoin(final_url, base_tag["href"]) if base_tag else final_url
        structured_navigation = _structured_navigation_links(soup, resolve_base)
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()

        controller_map = _menu_controller_map(soup, resolve_base)
        discovered_links: list[tuple[str, str, bool, str, int | None]] = []
        structured_level = min(
            self.navigation_max_depth,
            (navigation_level if navigation_level is not None else 0) + 1,
        )
        for candidate, label in structured_navigation:
            discovered_links.append(
                (candidate, _clean_text(label, 500), True, final_url, structured_level)
            )
        for link in soup.find_all(["a", "area"], href=True):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            label = _clean_text(
                link.get_text(" ")
                or link.get("aria-label", "")
                or link.get("title", "")
                or link.get("alt", ""),
                500,
            )
            candidate = urljoin(resolve_base, href)
            is_navigation = _is_navigation_anchor(link)
            logical_parent = final_url
            child_navigation_level: int | None = None
            if is_navigation:
                logical_parent = _logical_navigation_parent(
                    link, soup, resolve_base, final_url, controller_map
                )
                dom_level = _navigation_dom_level(link)
                is_global_navigation = _is_global_navigation_anchor(link)
                if is_global_navigation and logical_parent == final_url:
                    logical_parent = self.base_url
                base_level = (
                    0
                    if is_global_navigation
                    else (navigation_level if navigation_level is not None else 0)
                )
                child_navigation_level = min(
                    self.navigation_max_depth,
                    max(base_level + 1, dom_level),
                )
            elif navigation_level is not None and _important_navigation_hint(candidate, label):
                child_navigation_level = min(
                    self.navigation_max_depth, navigation_level + 1
                )
            discovered_links.append(
                (candidate, label, is_navigation, logical_parent, child_navigation_level)
            )

        data_attributes = ("data-href", "data-url", "data-link", "data-destination")
        for element in soup.find_all(True):
            raw_candidates: list[str] = []
            for attribute in data_attributes:
                raw = str(element.get(attribute, "") or "").strip()
                if raw:
                    raw_candidates.append(raw)
            onclick_candidate = _onclick_url(str(element.get("onclick", "") or ""))
            if onclick_candidate:
                raw_candidates.append(onclick_candidate)
            if element.name == "option":
                option_value = str(element.get("value", "") or "").strip()
                if option_value.startswith(("http://", "https://", "/")):
                    raw_candidates.append(option_value)
            if not raw_candidates:
                continue
            label = _clean_text(
                element.get_text(" ")
                or element.get("aria-label", "")
                or element.get("title", ""),
                500,
            )
            is_navigation = _is_navigation_anchor(element)
            for raw in raw_candidates:
                candidate = urljoin(resolve_base, raw)
                logical_parent = final_url
                child_navigation_level = None
                if is_navigation:
                    logical_parent = _logical_navigation_parent(
                        element, soup, resolve_base, final_url, controller_map
                    )
                    is_global_navigation = _is_global_navigation_anchor(element)
                    if is_global_navigation and logical_parent == final_url:
                        logical_parent = self.base_url
                    base_level = (
                        0
                        if is_global_navigation
                        else (navigation_level if navigation_level is not None else 0)
                    )
                    child_navigation_level = min(
                        self.navigation_max_depth,
                        max(base_level + 1, _navigation_dom_level(element)),
                    )
                discovered_links.append(
                    (candidate, label, is_navigation, logical_parent, child_navigation_level)
                )

        metadata_links: list[tuple[str, str]] = []
        for link in soup.find_all("link", href=True):
            rel = " ".join(link.get("rel") or []).lower()
            if any(term in rel for term in ("canonical", "alternate", "sitemap")):
                metadata_links.append((urljoin(resolve_base, link["href"]), rel or "metadata link"))

        title = _clean_text(soup.title.get_text(" ") if soup.title else "", 1000)
        meta = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = _clean_text(meta.get("content", "") if meta else "", 2000)
        h1_tag = soup.find("h1")
        h1 = _clean_text(h1_tag.get_text(" ") if h1_tag else "", 1000)

        content_root = (
            soup.find("main")
            or soup.find("article")
            or soup.find(attrs={"role": "main"})
            or soup.body
            or soup
        )
        content_copy = BeautifulSoup(str(content_root), "html.parser")
        for tag in content_copy.find_all(["nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        content = _clean_text(content_copy.get_text(" "), 7000)
        self._record_page(
            url, final_url, response.status_code, content_type, depth,
            title, description, h1, content, anchor_text, parent_url, False,
        )

        discovered_links.sort(
            key=lambda item: (
                item[4] is None,
                item[4] if item[4] is not None else 99,
                not item[2],
            )
        )
        for candidate, label, is_navigation, logical_parent, child_navigation_level in discovered_links:
            inherited_level = None
            if navigation_level is not None and navigation_level < self.navigation_max_depth:
                inherited_level = navigation_level + 1
            self._discover(
                candidate,
                logical_parent if is_navigation else final_url,
                label,
                depth + 1,
                priority_queue=is_navigation or child_navigation_level is not None,
                navigation_level=child_navigation_level,
                inherit_navigation_level=inherited_level,
            )

        for candidate, rel in metadata_links:
            self._discover(candidate, final_url, rel, depth + 1)

    def _parse_sitemap(self, body: bytes, sitemap_url: str, depth: int) -> None:
        try:
            if sitemap_url.lower().endswith(".gz") or body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            text = body.decode("utf-8", errors="ignore")
            locations = re.findall(r"<loc[^>]*>\s*(.*?)\s*</loc>", text, flags=re.I | re.S)
            for raw in locations:
                location = unescape(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", raw, flags=re.S)).strip()
                if location:
                    self._discover(location, sitemap_url, "Sitemap URL", min(depth + 1, self.max_depth), force_queue=True)
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("Unable to parse sitemap %s: %s", sitemap_url, exc)

    def _record_page(
        self, original_url: str, final_url: str, status: int, content_type: str, depth: int,
        title: str, description: str, h1: str, content: str, anchor_text: str,
        parent_url: str | None, discovery_only: bool,
    ) -> None:
        normalized = normalize_url(original_url) or original_url
        classification = classify_url(
            url=final_url,
            anchor_text=anchor_text,
            title=title,
            h1=h1,
            meta_description=description,
            content=content,
            parent_url=parent_url,
            file_extension=extension_from_url(final_url),
            relevant_threshold=self.relevant_threshold,
            review_threshold=self.review_threshold,
        )
        storage = classification.as_storage()

        record = DiscoveredUrl.objects.filter(job_id=self.job_id, normalized_url=normalized).first()
        if not record:
            return

        existing_navigation_level = _navigation_level_from_reason(record.classification_reason)
        if existing_navigation_level is not None:
            if storage["decision_status"] in {"excluded", "pending"}:
                storage["decision_status"] = "review"
            storage["is_discovery_only"] = False
            storage["classification_reason"] = _merge_navigation_reason(
                storage.get("classification_reason"), existing_navigation_level
            )
        if discovery_only:
            storage["is_discovery_only"] = True
            if storage["decision_status"] == "relevant":
                storage["decision_status"] = "review"
        else:
            storage["is_discovery_only"] = storage["decision_status"] == "excluded"
        digest_source = content or title or final_url
        content_hash = hashlib.sha256(digest_source.encode("utf-8", errors="ignore")).hexdigest()

        record.final_url = final_url
        record.page_title = title or None
        record.meta_description = description or None
        record.h1 = h1 or None
        record.content_type = content_type[:255] or None
        record.http_status = status
        record.crawl_depth = depth
        record.content_hash = content_hash
        record.crawled_at = utcnow()

        # Never downgrade a URL that was already identified from a strong
        # anchor or parent path. Fetching the page adds evidence; it should
        # not erase useful discovery context.
        rank = {"excluded": 0, "pending": 0, "review": 1, "relevant": 2}
        old_rank = rank.get(record.decision_status, 0)
        new_rank = rank.get(str(storage["decision_status"]), 0)
        use_new = (
            new_rank > old_rank
            or (new_rank == old_rank and float(storage["relevance_score"]) >= float(record.relevance_score or 0))
        )
        if use_new:
            for key, value in storage.items():
                setattr(record, key, value)
        elif not discovery_only and record.decision_status in {"relevant", "review"}:
            record.is_discovery_only = False
        record.save()

        DiscoveryJob.objects.filter(id=self.job_id).update(
            pages_crawled=F("pages_crawled") + 1, current_url=final_url
        )
        self._refresh_counts()

    def _mark_fetch_failure(self, url: str, message: str, http_status: int | None = None, excluded: bool = True) -> None:
        normalized = normalize_url(url) or url
        record = DiscoveredUrl.objects.filter(job_id=self.job_id, normalized_url=normalized).first()
        if record:
            record.exclusion_reason = message[:2000]
            record.http_status = http_status
            record.crawled_at = utcnow()
            if excluded and record.decision_status == "pending":
                record.decision_status = "review"
            record.save()
        DiscoveryJob.objects.filter(id=self.job_id).update(failed_count=F("failed_count") + 1, current_url=url)
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        counts = {
            row["decision_status"]: row["n"]
            for row in DiscoveredUrl.objects.filter(job_id=self.job_id)
            .values("decision_status")
            .annotate(n=Count("id"))
        }
        DiscoveryJob.objects.filter(id=self.job_id).update(
            relevant_count=int(counts.get("relevant", 0)),
            review_count=int(counts.get("review", 0) + counts.get("pending", 0)),
            excluded_count=int(counts.get("excluded", 0)),
        )

    def _mark_running(self) -> None:
        DiscoveryJob.objects.filter(id=self.job_id).update(
            status="running", started_at=utcnow(), completed_at=None, error_message=""
        )

    def _finish(self, status: str, error: str | None = None) -> None:
        self._refresh_counts()
        DiscoveryJob.objects.filter(id=self.job_id).update(
            status=status, error_message=error or "", completed_at=utcnow()
        )

    def _job_status(self) -> str:
        return DiscoveryJob.objects.filter(id=self.job_id).values_list("status", flat=True).first() or "stop_requested"

    def _pages_crawled(self) -> int:
        return DiscoveryJob.objects.filter(id=self.job_id).values_list("pages_crawled", flat=True).first() or 0
