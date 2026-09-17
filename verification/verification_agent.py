from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple


# Generic placeholder values extractors fall back to when they genuinely
# could not find something (e.g. resume_parser.py defaults name to
# "Student" rather than leaving it blank). These must never be treated as a
# "found" value -- otherwise a parser failure gets reported to the student
# as a *mismatch* instead of *not found*, which is misleading about what
# actually happened.
PLACEHOLDER_VALUES = {
    "student", "n a", "na", "unknown", "none", "not provided",
    "not available", "candidate", "user", "not stated",
}

ALL_SOURCES = ("resume", "github", "linkedin")


def compute_missing_sources(expected_name: str, sources_present: Dict[str, bool]) -> List[str]:
    """Shared between both engines (AI and rule-based fallback) so they never
    disagree on what counts as "not enough uploaded yet to verify"."""
    missing: List[str] = []
    if not expected_name:
        missing.append("profile_name")
    for source in ALL_SOURCES:
        if not sources_present.get(source):
            missing.append(source)
    return missing


@dataclass(frozen=True)
class VerificationCandidate:
    """
    One flagged disagreement, either between the profile and a single
    source (rule-based engine) or across multiple sources at once
    (AI engine -- e.g. "resume and LinkedIn disagree on institution").
    """

    dimension: str            # e.g. "name", "email", "institution", "major", "experience", or an
                               # AI-invented slug for something that doesn't fit the fixed set
    sources: Tuple[str, ...]  # which of resume/github/linkedin/profile participate in this disagreement
    severity: str              # "moderate" | "high"
    expected: str
    found: str
    message: str
    confidence: Optional[float] = None

    @property
    def key(self) -> str:
        """Stable identity for this exact disagreement, used to diff against
        previously-raised items across reanalyses (see verification/services.py)."""
        return f"{self.dimension}:{'+'.join(sorted(self.sources))}"


class VerificationAgent:
    """
    Deterministic field-by-field comparator. This is now the FALLBACK
    engine -- verification/ai_agent.py's holistic LLM judge is the primary
    path (see verification/services.py). Kept and still exercised because
    an LLM call can fail (no API key, network error, malformed response),
    and verification should degrade to "still works, just cruder" rather
    than stop functioning entirely -- the same primary/fallback split
    agents/github_agent.py already uses for its own assessment step.

    Pure logic: takes plain dicts/strings in, returns VerificationCandidate
    objects out. No Django/DB dependency, so it can be unit tested in
    isolation from persistence.
    """

    NAME_KEYS = [
        "name", "full_name", "fullName", "display_name", "displayName",
        "profile_name", "candidate_name", "student_name", "real_name",
    ]
    EMAIL_KEYS = ["email"]
    INSTITUTION_KEYS = ["institution", "undergraduate_institution"]
    MAJOR_KEYS = ["major", "undergraduate_major"]

    NAME_MATCH_THRESHOLD = 0.86
    TOPIC_MATCH_THRESHOLD = 0.4
    WORK_MONTHS_THRESHOLD = 6

    # Pure connector words dropped before institution/major token comparison.
    # Deliberately does NOT include domain words like "engineering",
    # "science", or "university" -- those often carry the exact distinction
    # that matters ("Computer Engineering" vs "Computer Science" must NOT
    # be treated as the same major just because they share "Computer").
    TOPIC_CONNECTOR_WORDS = {"of", "and", "the", "in", "for", "at"}

    SOURCE_LABELS = {"resume": "your resume", "github": "GitHub", "linkedin": "LinkedIn"}

    # ------------------------------------------------------------------
    # Normalization / similarity primitives
    # ------------------------------------------------------------------

    def _normalize(self, text: Any, strip_honorifics: bool = False) -> str:
        value = str(text or "").strip().lower()
        if strip_honorifics:
            value = re.sub(r"\b(mr|mrs|ms|miss|dr|prof)\.?\b", " ", value)
        value = re.sub(r"[^a-z0-9\s]", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value

    def _similarity(self, a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()

    def _field_value(self, data: Any, keys: List[str]) -> str:
        """Top-level-only lookup -- deliberately not recursive. Every source
        dict here also has plenty of *unrelated* nested keys with the same
        names (a repo's "name", a language's "name", a project's "title"),
        so a recursive search would happily report a programming language
        as the student's name. Each producer is expected to expose the
        value directly under one of `keys` at the top level."""
        if not isinstance(data, dict):
            return ""
        for key in keys:
            value = data.get(key)
            if isinstance(value, (str, int, float)):
                value = str(value).strip()
                if value and value.lower() not in PLACEHOLDER_VALUES:
                    return value
        return ""

    def names_match(self, expected_name: Any, found_name: Any) -> bool:
        expected = self._normalize(expected_name, strip_honorifics=True)
        found = self._normalize(found_name, strip_honorifics=True)

        if not expected or not found:
            return False
        if expected == found:
            return True

        expected_tokens = set(expected.split())
        found_tokens = set(found.split())

        if len(expected_tokens) >= 2 and expected_tokens.issubset(found_tokens):
            return True
        if len(found_tokens) >= 2 and found_tokens.issubset(expected_tokens):
            return True

        return self._similarity(expected, found) >= self.NAME_MATCH_THRESHOLD

    def emails_match(self, expected_email: Any, found_email: Any) -> bool:
        return str(expected_email or "").strip().lower() == str(found_email or "").strip().lower()

    def _topic_tokens(self, text: Any) -> set:
        base = self._normalize(text)
        return {t for t in base.split() if t and t not in self.TOPIC_CONNECTOR_WORDS}

    def _topic_similarity(self, a: Any, b: Any) -> float:
        """Jaccard overlap on word tokens, not character-level similarity.
        Character-level ratio rewards any shared substring regardless of
        word boundaries, so "Computer Engineering" vs "Electrical
        Engineering" scores 0.71 on shared characters alone (" engineering")
        even though they're different majors. Token overlap only credits
        actually-shared words: {computer, engineering} vs {electrical,
        engineering} share 1 of 3 unique tokens -> 0.33, correctly below
        threshold."""
        tokens_a = self._topic_tokens(a)
        tokens_b = self._topic_tokens(b)
        if not tokens_a or not tokens_b:
            return 0.0
        union = tokens_a | tokens_b
        return len(tokens_a & tokens_b) / len(union) if union else 0.0

    def text_matches(self, expected: Any, found: Any) -> bool:
        """Looser than names_match -- for institution/major, which vary a lot
        in how they're written ("VJTI" vs "Veermata Jijabai Technological
        Institute"). Deliberately permissive: cost of a false positive here
        is one extra confirm-click, cost of a false negative is a real
        discrepancy going unflagged, so this leans toward flagging."""
        expected_n = self._normalize(expected)
        found_n = self._normalize(found)
        if not expected_n or not found_n:
            return False
        if expected_n == found_n or expected_n in found_n or found_n in expected_n:
            return True
        return self._topic_similarity(expected, found) >= self.TOPIC_MATCH_THRESHOLD

    # ------------------------------------------------------------------
    # Per-field checks
    # ------------------------------------------------------------------

    def _name_candidates(
        self, expected_name: str, resume_data: Dict, github_data: Dict, linkedin_data: Dict
    ) -> List[VerificationCandidate]:
        if not expected_name:
            return []
        candidates = []
        for source, data in (("resume", resume_data), ("github", github_data), ("linkedin", linkedin_data)):
            found = self._field_value(data, self.NAME_KEYS)
            if found and not self.names_match(expected_name, found):
                candidates.append(VerificationCandidate(
                    dimension="name", sources=(source,), severity="moderate",
                    expected=expected_name, found=found,
                    message=(
                        f"Your profile name is \"{expected_name}\" but "
                        f"{self.SOURCE_LABELS[source]} shows \"{found}\"."
                    ),
                ))
        return candidates

    def _email_candidates(
        self, expected_email: str, resume_data: Dict, github_verified_email: str
    ) -> List[VerificationCandidate]:
        if not expected_email:
            return []
        candidates = []

        resume_email = self._field_value(resume_data, self.EMAIL_KEYS)
        if resume_email and not self.emails_match(expected_email, resume_email):
            candidates.append(VerificationCandidate(
                dimension="email", sources=("resume",), severity="moderate",
                expected=expected_email, found=resume_email,
                message=f"Your profile email is \"{expected_email}\" but your resume shows \"{resume_email}\".",
            ))

        if github_verified_email and not self.emails_match(expected_email, github_verified_email):
            candidates.append(VerificationCandidate(
                dimension="email", sources=("github",), severity="moderate",
                expected=expected_email, found=github_verified_email,
                message=(
                    f"Your profile email is \"{expected_email}\" but the email on your "
                    f"connected GitHub account is \"{github_verified_email}\"."
                ),
            ))
        return candidates

    def _best_linkedin_match(self, expected: str, entries: Any, entry_key: str) -> Optional[str]:
        """Across a LinkedIn education list, finds the entry whose `entry_key`
        (institution or field_of_study) is closest to `expected`. Returns
        that value if it's still below the match threshold (i.e. worth
        flagging), else None."""
        if not isinstance(entries, list) or not entries:
            return None
        best_value, best_score = None, 0.0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            value = str(entry.get(entry_key) or "").strip()
            if not value:
                continue
            score = self._topic_similarity(expected, value)
            if score > best_score:
                best_score, best_value = score, value
        if best_value and best_score < self.TOPIC_MATCH_THRESHOLD:
            return best_value
        return None

    def _institution_candidates(self, expected_institution: str, resume_data: Dict, linkedin_data: Dict) -> List[VerificationCandidate]:
        if not expected_institution:
            return []
        candidates = []

        resume_institution = self._field_value(resume_data, self.INSTITUTION_KEYS)
        if resume_institution and not self.text_matches(expected_institution, resume_institution):
            candidates.append(VerificationCandidate(
                dimension="institution", sources=("resume",), severity="moderate",
                expected=expected_institution, found=resume_institution,
                message=(
                    f"Your profile lists \"{expected_institution}\" as your institution but "
                    f"your resume shows \"{resume_institution}\"."
                ),
            ))

        linkedin_education = linkedin_data.get("education") if isinstance(linkedin_data, dict) else None
        found = self._best_linkedin_match(expected_institution, linkedin_education, "institution")
        if found:
            candidates.append(VerificationCandidate(
                dimension="institution", sources=("linkedin",), severity="moderate",
                expected=expected_institution, found=found,
                message=(
                    f"Your profile lists \"{expected_institution}\" as your institution but "
                    f"LinkedIn shows \"{found}\"."
                ),
            ))
        return candidates

    def _major_candidates(self, expected_major: str, resume_data: Dict, linkedin_data: Dict) -> List[VerificationCandidate]:
        if not expected_major:
            return []
        candidates = []

        resume_major = self._field_value(resume_data, self.MAJOR_KEYS)
        if resume_major and not self.text_matches(expected_major, resume_major):
            candidates.append(VerificationCandidate(
                dimension="major", sources=("resume",), severity="moderate",
                expected=expected_major, found=resume_major,
                message=f"Your profile lists \"{expected_major}\" as your major but your resume shows \"{resume_major}\".",
            ))

        linkedin_education = linkedin_data.get("education") if isinstance(linkedin_data, dict) else None
        found = self._best_linkedin_match(expected_major, linkedin_education, "field_of_study")
        if found:
            candidates.append(VerificationCandidate(
                dimension="major", sources=("linkedin",), severity="moderate",
                expected=expected_major, found=found,
                message=f"Your profile lists \"{expected_major}\" as your major but LinkedIn shows \"{found}\".",
            ))
        return candidates

    def _experience_candidates(self, expected_work_months: Optional[float], resume_data: Dict) -> List[VerificationCandidate]:
        if not isinstance(resume_data, dict):
            return []
        resume_months = resume_data.get("work_months")
        if resume_months is None:
            return []
        try:
            resume_months = float(resume_months)
        except (TypeError, ValueError):
            return []

        baseline = float(expected_work_months or 0)
        if abs(baseline - resume_months) < self.WORK_MONTHS_THRESHOLD:
            return []

        return [VerificationCandidate(
            dimension="experience", sources=("resume",), severity="moderate",
            expected=f"{baseline:g} months", found=f"{resume_months:g} months",
            message=(
                f"Your profile shows {baseline:g} months of work experience but your resume "
                f"implies {resume_months:g} months."
            ),
        )]

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def analyze_rule_based(
        self,
        *,
        expected_name: str,
        expected_email: str = "",
        expected_institution: str = "",
        expected_major: str = "",
        expected_work_months: Optional[float] = None,
        resume_data: Optional[Dict[str, Any]] = None,
        github_data: Optional[Dict[str, Any]] = None,
        linkedin_data: Optional[Dict[str, Any]] = None,
        github_verified_email: str = "",
        sources_present: Optional[Dict[str, bool]] = None,
    ) -> Dict[str, Any]:
        """
        Runs every check against whatever data is available. Sources that
        are still missing don't block checks against the sources that ARE
        present -- a student who's uploaded a resume but not connected
        GitHub yet can still see a resume mismatch immediately, rather than
        waiting until all three are done.

        Unlike the AI engine, this compares each field independently and
        has no notion of "this kind of difference is expected for this
        source" (e.g. it will flag a GitHub email difference the same way
        it flags a resume one) -- it's the coarser fallback, not the
        primary path. See verification/ai_agent.py for the holistic judge.
        """
        resume_data = resume_data if isinstance(resume_data, dict) else {}
        github_data = github_data if isinstance(github_data, dict) else {}
        linkedin_data = linkedin_data if isinstance(linkedin_data, dict) else {}
        sources_present = sources_present or {}

        missing_sources = compute_missing_sources(expected_name, sources_present)

        candidates: List[VerificationCandidate] = []
        candidates += self._name_candidates(expected_name, resume_data, github_data, linkedin_data)
        candidates += self._email_candidates(expected_email, resume_data, github_verified_email)
        candidates += self._institution_candidates(expected_institution, resume_data, linkedin_data)
        candidates += self._major_candidates(expected_major, resume_data, linkedin_data)
        candidates += self._experience_candidates(expected_work_months, resume_data)

        return {"missing_sources": missing_sources, "candidates": candidates}
