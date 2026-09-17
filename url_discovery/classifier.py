from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable
from urllib.parse import unquote

import yaml

from url_discovery.rules_data import RULES_DIR


@dataclass(slots=True)
class ClassificationResult:
    primary_category: str | None
    secondary_categories: list[str]
    category_scores: dict[str, float]
    relevance_score: float
    confidence: float
    decision_status: str
    is_discovery_only: bool
    reasons: list[str]

    def as_storage(self) -> dict:
        return {
            "primary_category": self.primary_category,
            "secondary_categories_json": json.dumps(self.secondary_categories, ensure_ascii=False),
            "category_scores_json": json.dumps(self.category_scores, ensure_ascii=False),
            "relevance_score": self.relevance_score,
            "confidence": self.confidence,
            "decision_status": self.decision_status,
            "is_discovery_only": self.is_discovery_only,
            "classification_reason": "; ".join(self.reasons[:10]),
        }


@lru_cache(maxsize=1)
def category_config() -> dict:
    path = RULES_DIR / "categories.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("categories", {})


def category_labels() -> list[str]:
    return [str(config["label"]) for config in category_config().values()]


def clear_category_cache() -> None:
    category_config.cache_clear()


def _clean(value: str | None) -> str:
    """Create word-boundary-friendly text from titles, URLs and page content."""
    decoded = unquote(value or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", decoded).strip()


def _keyword_matches(text: str, keywords: Iterable[str]) -> list[str]:
    """Match complete terms/phrases instead of unsafe substrings.

    For example, ``form`` must not match ``information`` and ``program`` must
    not match ``programming``. URL separators are normalized to spaces first.
    """
    if not text:
        return []

    matches: list[str] = []
    for keyword in keywords:
        item = _clean(str(keyword))
        if not item:
            continue
        pattern = r"(?<![a-z0-9])" + r"\s+".join(re.escape(part) for part in item.split()) + r"(?![a-z0-9])"
        if re.search(pattern, text):
            matches.append(str(keyword).lower())
    return matches


def _field_score(matches: list[str], weight: float) -> float:
    if not matches:
        return 0.0
    unique = len(set(matches))
    # One clear structural signal is useful. More matches quickly approach the
    # field's full weight without allowing keyword stuffing to dominate.
    multiplier = min(1.0, 0.70 + (unique - 1) * 0.15)
    return weight * multiplier


def classify_url(
    *,
    url: str,
    anchor_text: str | None = None,
    title: str | None = None,
    h1: str | None = None,
    meta_description: str | None = None,
    content: str | None = None,
    parent_url: str | None = None,
    file_extension: str | None = None,
    relevant_threshold: float = 30,
    review_threshold: float = 1,
) -> ClassificationResult:
    """Classify a university URL with a recall-first, evidence-based policy.

    The earlier implementation required a very high numeric score, so pages
    with a clear URL/title signal could still be excluded. This version treats
    URL, anchor, title, H1 and parent-path matches as direct evidence. Content-
    only matches remain in Review unless multiple strong phrases are present.
    """
    fields = {
        "url": (_clean(url), 30.0),
        "anchor": (_clean(anchor_text), 26.0),
        "title": (_clean(title), 26.0),
        "h1": (_clean(h1), 22.0),
        "meta": (_clean(meta_description), 12.0),
        "content": (_clean((content or "")[:7000]), 14.0),
        "parent": (_clean(parent_url), 10.0),
    }

    all_scores: dict[str, float] = {}
    reason_map: dict[str, list[str]] = {}
    evidence_map: dict[str, dict[str, object]] = {}

    for _key, config in category_config().items():
        label = str(config["label"])
        strong = [str(item) for item in config.get("strong_keywords", [])]
        aliases = [str(item) for item in config.get("url_keywords", [])]
        negatives = [str(item) for item in config.get("negative_keywords", [])]
        score = 0.0
        reasons: list[str] = []
        matched_fields: set[str] = set()
        strong_fields: set[str] = set()
        matched_terms: set[str] = set()

        for field_name, (field_text, weight) in fields.items():
            # Short category aliases such as "degree", "tuition" and "housing"
            # are valid evidence in structural fields, not only in the URL.
            keywords = list(strong)
            if field_name in {"url", "anchor", "title", "h1", "meta", "parent"}:
                keywords.extend(aliases)

            matches = _keyword_matches(field_text, keywords)
            strong_matches = _keyword_matches(field_text, strong)
            gained = _field_score(matches, weight)
            score += gained

            if matches:
                matched_fields.add(field_name)
                matched_terms.update(matches)
                reasons.append(
                    f"{field_name} matched {', '.join(sorted(set(matches))[:4])} (+{gained:.0f})"
                )
            if strong_matches:
                strong_fields.add(field_name)

        combined = " ".join(value for value, _weight in fields.values())
        negative_matches = _keyword_matches(combined, negatives)
        if negative_matches:
            # Negative phrases reduce confidence but must not wipe out several
            # clear admissions/program/support signals.
            penalty = min(20.0, 8.0 * len(set(negative_matches)))
            if len(matched_fields) >= 2:
                penalty *= 0.5
            score -= penalty
            reasons.append(
                f"negative match {', '.join(sorted(set(negative_matches))[:3])} (-{penalty:.0f})"
            )

        is_document = bool(
            file_extension
            and file_extension.lower()
            in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx"}
        )
        if is_document and matched_fields:
            score += 8.0
            reasons.append("supported downloadable document (+8)")

        final_score = round(max(0.0, min(100.0, score)), 1)
        all_scores[label] = final_score
        reason_map[label] = reasons
        evidence_map[label] = {
            "matched_fields": matched_fields,
            "strong_fields": strong_fields,
            "matched_terms": matched_terms,
            "is_document": is_document,
        }

    ranked = sorted(all_scores.items(), key=lambda pair: pair[1], reverse=True)
    primary, top_score = ranked[0] if ranked else (None, 0.0)
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    evidence = evidence_map.get(primary or "", {})
    matched_fields = set(evidence.get("matched_fields", set()))
    strong_fields = set(evidence.get("strong_fields", set()))
    matched_terms = set(evidence.get("matched_terms", set()))
    is_document = bool(evidence.get("is_document", False))

    secondary_floor = max(review_threshold, 1.0)
    secondary = [
        label
        for label, score in ranked[1:]
        if score >= secondary_floor and score >= top_score - 28
    ][:4]

    direct_fields = {"url", "anchor", "title", "h1"}
    contextual_fields = direct_fields | {"parent", "meta"}
    direct_evidence = bool(matched_fields & direct_fields)
    contextual_evidence = bool(matched_fields & contextual_fields)
    multiple_evidence = len(matched_fields) >= 2 or len(matched_terms) >= 2
    multiple_strong_content = "content" in strong_fields and len(matched_terms) >= 2

    if top_score <= 0:
        status = "excluded"
    elif (
        top_score >= relevant_threshold
        or direct_evidence
        or (contextual_evidence and multiple_evidence)
        or multiple_strong_content
        or (is_document and contextual_evidence)
    ):
        status = "relevant"
    elif top_score >= review_threshold:
        status = "review"
    else:
        status = "excluded"

    # Review records are useful candidates and must remain visible/exportable.
    # Only truly unmatched pages are discovery-only by default.
    discovery_only = status == "excluded"
    confidence = round(max(0.0, min(100.0, 55.0 + top_score - second_score)), 1)
    reasons = list(reason_map.get(primary or "", []))
    if status == "relevant" and top_score < relevant_threshold:
        reasons.append("included because a direct URL/title/heading/category signal was found")
    if status == "review":
        reasons.append("possible useful page; retained for review instead of exclusion")
    if not reasons:
        reasons = ["No student-focused university category signal found"]

    return ClassificationResult(
        primary_category=primary if top_score > 0 else None,
        secondary_categories=secondary,
        category_scores=all_scores,
        relevance_score=top_score,
        confidence=confidence,
        decision_status=status,
        is_discovery_only=discovery_only,
        reasons=reasons,
    )


def crawl_priority(url: str, anchor_text: str | None = None) -> int:
    result = classify_url(
        url=url,
        anchor_text=anchor_text,
        relevant_threshold=30,
        review_threshold=1,
    )
    return int(result.relevance_score * 10)
