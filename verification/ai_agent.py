from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import anthropic

from verification.verification_agent import (
    ALL_SOURCES,
    VerificationCandidate,
    compute_missing_sources,
)

MODEL = "claude-haiku-4-5-20251001"

VALID_SEVERITIES = {"moderate", "high"}

SYSTEM_PROMPT = """You are an integrity verification analyst for Kormic Commons, a graduate \
admissions platform. A student has declared a profile and may have uploaded a resume, connected \
a GitHub account, and/or uploaded LinkedIn screenshots. Your job is to decide whether there is \
genuine reason to question whether these sources are honest and consistent -- NOT to mechanically \
diff every field and flag every difference you see.

Judge holistically, across all sources together, the way a careful human reviewer would -- not by \
walking a fixed checklist of fields. Understand what kind of evidence each source actually is:

- "profile": what the student directly typed in. The baseline claim.
- "resume": a formal document the student chose to upload. Its name, email, education, and \
  experience should normally agree closely with the profile and with LinkedIn, because all three \
  are the student's own self-curated professional identity.
- "linkedin": another self-curated professional profile, the same kind of evidence as the resume. \
  It should also agree closely with the resume and profile on factual claims like institution, \
  degree, dates, and employers.
- "github": a developer account, not an identity document. Its display name, username, and email \
  are frequently and *legitimately* different from a person's formal identity -- pseudonymous \
  handles, a personal email instead of a university one, or no public email at all are all normal \
  and expected. A GitHub name/email difference on its own, with nothing else wrong, is NOT \
  suspicious and must not be flagged. Only raise something about GitHub if it points to a \
  completely different, clearly identifiable real person's name that also fails to match every \
  other source, or something else that is actually alarming.

Examples of what SHOULD be flagged (real, meaningful disagreements worth asking the student about):
- The resume and LinkedIn show different institutions, degrees, or majors for what's supposed to be \
  the same education.
- Work experience duration or employer differs substantially between resume and profile/LinkedIn.
- Graduation years or a claimed timeline that doesn't add up (e.g. work experience predating a \
  degree that's still years from finishing).
- A name on the resume or LinkedIn that reads as a completely different actual person, not a \
  formatting variant of the profile name.
- Skills or achievements confidently claimed on the resume that are flatly contradicted elsewhere.

Examples of what should NOT be flagged (do not raise these):
- GitHub name, username, or email differing from the profile/resume/LinkedIn, when nothing else is \
  wrong -- this is normal for a developer account.
- Minor spelling, formatting, or abbreviation differences that clearly refer to the same thing \
  ("VJTI" vs "VJTI Mumbai", "CS" vs "Computer Science", "Jon" vs "Jonathan").
- A field that is simply missing or blank in one source -- that is a data-completeness gap, not a \
  mismatch. Do not invent a comparison against nothing.
- Differences that are fully explained by normal degree progression (e.g. LinkedIn shows a Master's \
  in progress while the resume covers the completed Bachelor's).

Be conservative. If you don't see a genuine, meaningful inconsistency, do not invent one just to \
have something to report -- returning zero findings is the correct and expected outcome for a \
clean, consistent profile. Only flag what a reasonable admissions reviewer would actually want to \
ask the student about.

You will also be given a list of issues still open from a previous analysis (awaiting the student).
This exists ONLY so that, if the same disagreement is still present in the current data, you reuse \
the exact same "dimension" and "sources_involved" for it -- so it's recognized as the same item, not \
duplicated. It is NOT evidence to reason about on its own: judge the CURRENT resume/github/linkedin/ \
profile data strictly on its own merits, exactly as if you were seeing this student for the first \
time. If a field that used to disagree now matches, that is simply a non-issue in the current data --\
do not comment on the fact that it changed, do not treat "this used to say X, now says Y" as itself \
suspicious. Only what the CURRENT snapshot actually shows determines whether a disagreement exists.

Return ONLY a JSON array (no markdown fences, no prose before or after). Each element:
{
  "dimension": short lowercase_snake_case slug -- prefer one of \
["name","email","institution","major","graduation_year","work_experience","timeline","identity","skills"] \
when it genuinely fits, otherwise invent a new short slug,
  "sources_involved": array, each item one of "resume", "github", "linkedin" -- every source that \
participates in this specific disagreement (never include "profile" here, it's always the implicit baseline),
  "severity": "moderate" (a real factual disagreement worth confirming) or "high" (suggests the \
sources may not all belong to the same person),
  "expected_summary": short plain-text summary of what the profile (or the primary source) claims,
  "found_summary": short plain-text summary of what the conflicting source(s) actually show,
  "message": one or two plain, neutral sentences addressed to the student, explaining the specific \
discrepancy and inviting them to confirm or clarify it -- never accusatory
}
If there is nothing worth flagging, return []."""


# Bounds the verification call so a hung upstream request can't hold the
# calling worker indefinitely.
ANTHROPIC_CLIENT_TIMEOUT_SECONDS = 120.0


def _get_anthropic_client() -> anthropic.Anthropic:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY not found. Falling back to rule-based verification.")
    return anthropic.Anthropic(timeout=ANTHROPIC_CLIENT_TIMEOUT_SECONDS, max_retries=1)


def _clean_model_json_array(raw: str) -> str:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()
    first = text.find("[")
    last = text.rfind("]")
    if first != -1 and last != -1 and last > first:
        text = text[first:last + 1]
    return text.strip()


class AIVerificationAgent:
    """
    Holistic LLM judge: looks at the profile plus resume/GitHub/LinkedIn
    data together and decides what's actually worth asking the student
    about, understanding that different sources carry different kinds of
    evidence (see SYSTEM_PROMPT). This is the primary verification engine;
    verification_agent.VerificationAgent (deterministic field diffing) is
    the fallback used only if this call fails.
    """

    def _build_user_message(
        self,
        *,
        profile_facts: Dict[str, Any],
        resume_data: Dict[str, Any],
        github_data: Dict[str, Any],
        linkedin_data: Dict[str, Any],
        sources_present: Dict[str, bool],
        open_items_context: List[Dict[str, Any]],
    ) -> str:
        payload = {
            "student_profile": profile_facts,
            "sources_present": sources_present,
            "resume_data": resume_data or None,
            "github_data": github_data or None,
            "linkedin_data": linkedin_data or None,
            "previously_raised_still_open": open_items_context,
        }
        return "Analyze this student's submitted information:\n\n" + json.dumps(payload, indent=2, default=str)

    def _parse_candidates(self, raw_findings: Any) -> List[VerificationCandidate]:
        candidates: List[VerificationCandidate] = []
        if not isinstance(raw_findings, list):
            return candidates

        for finding in raw_findings:
            if not isinstance(finding, dict):
                continue

            dimension = str(finding.get("dimension") or "").strip().lower().replace(" ", "_")[:50]
            if not dimension:
                continue

            raw_sources = finding.get("sources_involved")
            sources = tuple(sorted({
                s for s in (raw_sources if isinstance(raw_sources, list) else [])
                if isinstance(s, str) and s in ALL_SOURCES
            }))
            if not sources:
                continue  # a finding must be attributable to at least one real source

            severity = str(finding.get("severity") or "").strip().lower()
            if severity not in VALID_SEVERITIES:
                severity = "moderate"

            message = str(finding.get("message") or "").strip()
            if not message:
                continue

            confidence = finding.get("confidence")
            try:
                confidence = float(confidence) if confidence is not None else None
            except (TypeError, ValueError):
                confidence = None

            candidates.append(VerificationCandidate(
                dimension=dimension,
                sources=sources,
                severity=severity,
                expected=str(finding.get("expected_summary") or "").strip(),
                found=str(finding.get("found_summary") or "").strip(),
                message=message,
                confidence=confidence,
            ))

        return candidates

    def analyze(
        self,
        *,
        expected_name: str,
        profile_facts: Dict[str, Any],
        resume_data: Optional[Dict[str, Any]] = None,
        github_data: Optional[Dict[str, Any]] = None,
        linkedin_data: Optional[Dict[str, Any]] = None,
        sources_present: Optional[Dict[str, bool]] = None,
        open_items_context: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        sources_present = sources_present or {}
        missing_sources = compute_missing_sources(expected_name, sources_present)

        client = _get_anthropic_client()
        user_message = self._build_user_message(
            profile_facts=profile_facts,
            resume_data=resume_data or {},
            github_data=github_data or {},
            linkedin_data=linkedin_data or {},
            sources_present=sources_present,
            open_items_context=open_items_context or [],
        )

        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text.strip()
        findings = json.loads(_clean_model_json_array(raw))
        candidates = self._parse_candidates(findings)

        return {"missing_sources": missing_sources, "candidates": candidates}
