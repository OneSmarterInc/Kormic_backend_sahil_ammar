# knowledge/scraper.py
# Scrapes university websites and extracts structured knowledge.
# Uses requests + BeautifulSoup for page fetching.
# Uses Claude to extract meaningful facts, with a safe fallback when Claude is unavailable.

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

import anthropic
import requests
from bs4 import BeautifulSoup
from rich.console import Console

console = Console()

MODEL = "claude-haiku-4-5-20251001"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# Bounds each extraction call so a hung upstream request can't hold a
# worker/Celery task indefinitely -- max_retries=1 
ANTHROPIC_CLIENT_TIMEOUT_SECONDS = 240.0


def _get_anthropic_client() -> anthropic.Anthropic:
    """
    Create Anthropic client only when fact extraction is requested.

    This prevents import/startup crashes if ANTHROPIC_API_KEY is missing.
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not found. Claude fact extraction is unavailable."
        )

    return anthropic.Anthropic(timeout=ANTHROPIC_CLIENT_TIMEOUT_SECONDS, max_retries=1)


def _clean_whitespace(text: str) -> str:
    """Normalize whitespace while keeping readable sentence spacing."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text


def _clean_json_array(raw: str) -> str:
    """
    Clean Claude output and isolate the JSON array.

    Handles:
    - plain JSON
    - ```json fenced JSON
    - small accidental text before/after JSON
    """
    text = str(raw or "").strip()

    if text.startswith("```"):
        text = text.replace("```json", "")
        text = text.replace("```", "")
        text = text.strip()

    first = text.find("[")
    last = text.rfind("]")

    if first != -1 and last != -1 and last > first:
        text = text[first:last + 1]

    return text.strip()


def _truncate(text: str, limit: int = 6000) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0]


def fetch_page(url: str, timeout: int = 15) -> str:
    """
    Fetch a page and return clean text content.

    Returns an empty string if the page cannot be fetched or parsed.
    """
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        if "html" not in content_type and "text" not in content_type:
            console.print(
                f"[yellow]Skipping non-text page: {url} ({content_type or 'unknown content type'})[/yellow]"
            )
            return ""

        soup = BeautifulSoup(response.text, "html.parser")

        # Remove noisy elements.
        for tag in soup(
            [
                "script",
                "style",
                "nav",
                "footer",
                "header",
                "aside",
                "iframe",
                "noscript",
                "svg",
                "form",
                "button",
            ]
        ):
            tag.decompose()

        # Prefer main/article content when available.
        main = soup.find("main") or soup.find("article") or soup.body or soup

        text = main.get_text(separator=" ", strip=True)
        clean_text = _clean_whitespace(text)

        return _truncate(clean_text, 8000)

    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", "unknown")
        console.print(f"[red]Failed to fetch {url}: HTTP {status}[/red]")
        return ""

    except requests.RequestException as exc:
        console.print(f"[red]Failed to fetch {url}: {exc}[/red]")
        return ""

    except Exception as exc:
        console.print(f"[red]Failed to parse {url}: {exc}[/red]")
        return ""


def _fallback_extract_facts(
    url: str,
    page_text: str,
    university_name: str,
) -> List[Dict[str, Any]]:
    """
    Rule-based fallback when Claude extraction fails.

    This avoids losing all scraped knowledge. The fact is marked as lower
    confidence later by scrape_university().
    """
    text = _clean_whitespace(page_text)

    if not text:
        return []

    keyword_groups = {
        "Admissions Requirements": [
            "requirement",
            "admission",
            "application",
            "transcript",
            "gpa",
            "gre",
            "gmat",
            "toefl",
            "ielts",
        ],
        "Tuition and Fees": [
            "tuition",
            "fee",
            "cost",
            "credit hour",
            "per credit",
        ],
        "Deadlines": [
            "deadline",
            "fall",
            "spring",
            "summer",
            "apply by",
        ],
        "Funding": [
            "assistantship",
            "fellowship",
            "scholarship",
            "funding",
            "stipend",
            "financial aid",
        ],
        "Program Details": [
            "computer science",
            "curriculum",
            "credits",
            "duration",
            "course",
            "degree",
            "master",
            "graduate",
        ],
        "Research Areas": [
            "research",
            "faculty",
            "laboratory",
            "lab",
            "cybersecurity",
            "artificial intelligence",
            "machine learning",
            "data science",
        ],
    }

    facts: List[Dict[str, Any]] = []

    lower_text = text.lower()

    for topic, keywords in keyword_groups.items():
        if any(keyword in lower_text for keyword in keywords):
            facts.append(
                {
                    "topic": topic,
                    "content": (
                        f"Relevant information for {university_name} was found on {url}. "
                        f"Page excerpt: {_truncate(text, 900)}"
                    ),
                    "confidence": 0.55,
                }
            )
            break

    if not facts:
        facts.append(
            {
                "topic": "Scraped Page Content",
                "content": (
                    f"Scraped page content from {university_name} official page {url}. "
                    f"Excerpt: {_truncate(text, 900)}"
                ),
                "confidence": 0.45,
            }
        )

    return facts


def extract_facts_from_page(
    url: str,
    page_text: str,
    university_name: str,
) -> List[Dict[str, Any]]:
    """
    Use Claude to extract structured facts from raw page content.

    Returns a list of dictionaries with:
    - topic
    - content
    - optional confidence

    If Claude is unavailable or invalid JSON is returned, falls back to a
    lower-confidence extracted page summary.
    """
    if not page_text or not page_text.strip():
        return []

    prompt = f"""Extract key facts about {university_name}'s graduate CS or computing-related program
from this webpage content.

Return ONLY a JSON array of objects with these fields:
[
  {{
    "topic": "short topic",
    "content": "specific fact from the page",
    "confidence": 0.0
  }}
]

Focus on extracting:
- GPA requirements
- GRE/GMAT requirements
- TOEFL/IELTS requirements
- Application deadlines
- Tuition and fees
- Program duration or credit requirements
- Available funding, RA, TA, fellowships, scholarships
- Research areas and faculty
- Program format, online/on-campus mode, or modality
- Focus areas/concentrations
- International student requirements
- Unique program features or regional connections

Rules:
- If a fact is not clearly stated on this page, do not invent it.
- Use confidence 0.9 for explicit facts, 0.7 for strongly supported facts, 0.5 for weak page-level summaries.
- Keep each content field concise but useful.
- Do not include duplicate facts.

URL:
{url}

PAGE CONTENT:
{_truncate(page_text, 6000)}
"""

    try:
        client = _get_anthropic_client()

        response = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )

        if not response.content:
            raise RuntimeError("Claude returned an empty response.")

        raw = response.content[0].text
        clean = _clean_json_array(raw)
        facts = json.loads(clean)

        if not isinstance(facts, list):
            raise ValueError("Claude did not return a JSON array.")

        cleaned_facts: List[Dict[str, Any]] = []

        for fact in facts:
            if not isinstance(fact, dict):
                continue

            topic = _clean_whitespace(fact.get("topic", ""))
            content = _clean_whitespace(fact.get("content", ""))

            if not topic or not content:
                continue

            try:
                confidence = float(fact.get("confidence", 0.9))
            except Exception:
                confidence = 0.9

            cleaned_facts.append(
                {
                    "topic": topic,
                    "content": content,
                    "confidence": max(0.0, min(1.0, confidence)),
                }
            )

        if cleaned_facts:
            return cleaned_facts

        raise ValueError("Claude returned no usable facts.")

    except Exception as exc:
        console.print(f"[yellow]Fact extraction failed for {url}: {exc}[/yellow]")
        return _fallback_extract_facts(url, page_text, university_name)


def _store_fact(kb, fact: Dict[str, Any], url: str, group_id: Optional[int] = None) -> bool:
    """
    Store a fact in the knowledge base.

    Tries with source_url first. Falls back if the current KB implementation
    does not support source_url.
    """
    topic = fact.get("topic")
    content = fact.get("content")

    if not topic or not content:
        return False

    try:
        confidence = float(fact.get("confidence", 0.9))
    except Exception:
        confidence = 0.9

    confidence = max(0.0, min(1.0, confidence))

    try:
        kb.store(
            topic=topic,
            content=content,
            source_type="scraped",
            source_url=url,
            confidence=confidence,
            group_id=group_id,
        )
        return True
    except TypeError:
        kb.store(
            topic=topic,
            content=content,
            source_type="scraped",
            confidence=confidence,
            group_id=group_id,
        )
        return True


def scrape_university(
    university_id: str,
    urls: List[str],
    university_name: str,
    kb,
    group_id: Optional[int] = None,
) -> int:
    """
    Scrape all target URLs for a university and store facts in its knowledge base.

    group_id tags every fact stored from this call with a KnowledgeGroup,
    e.g. when scraping a url_discovery cluster that was just approved for a
    specific department.

    Returns the total number of facts stored.
    """
    total_facts = 0
    seen = set()

    if not urls:
        return 0

    for index, url in enumerate(urls):
        console.print(
            f"  [dim]Scraping ({index + 1}/{len(urls)}): {url[:80]}...[/dim]"
        )

        page_text = fetch_page(url)

        if not page_text:
            continue

        facts = extract_facts_from_page(
            url=url,
            page_text=page_text,
            university_name=university_name,
        )

        for fact in facts:
            topic = _clean_whitespace(fact.get("topic", ""))
            content = _clean_whitespace(fact.get("content", ""))

            if not topic or not content:
                continue

            dedupe_key = (topic.lower(), content[:250].lower())

            if dedupe_key in seen:
                continue

            seen.add(dedupe_key)

            fact["topic"] = topic
            fact["content"] = content

            try:
                if _store_fact(kb, fact, url, group_id=group_id):
                    total_facts += 1
            except Exception as exc:
                console.print(f"[yellow]Could not store scraped fact from {url}: {exc}[/yellow]")

        # Be respectful to university servers.
        time.sleep(1.5)

    console.print(
        f"  [green]Scraping completed for {university_id}: {total_facts} fact(s) stored.[/green]"
    )

    return total_facts
