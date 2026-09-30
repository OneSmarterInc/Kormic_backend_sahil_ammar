from __future__ import annotations
import re
import unicodedata
from typing import TypedDict
from .sections import sections
from .upload_sections import restrict_fields, context_source
class ExtractionState(TypedDict, total=False):
    profile_id: int
    run_id: int
    documents: list[dict]
    cleaned_documents: list[dict]
    observations: list[dict]
    validated_observations: list[dict]
    merged_profile: dict
    successful_images: int

def clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = []
    for line in text.splitlines():
        clean = line.strip()
        if not clean:
            lines.append("")
            continue
        if UI_NOISE_LINE.fullmatch(clean):
            continue
        lines.append(clean)
    return "\n".join(lines).strip()

def _canonical(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = text.translate(str.maketrans({"–": "-", "—": "-", "’": "'", "‘": "'", "\u00ad": ""}))
    return re.sub(r"\s+", " ", text).strip().casefold()

def _evidence_supported(evidence: str, source: str) -> bool:
    if not evidence or not source:
        return False
    return _canonical(evidence) in _canonical(source)

def _value_supported(value: str | None, evidence: str) -> bool:
    if not value:
        return True
    return bool(re.search(r"(?<!\w)" + re.escape(_canonical(value)) + r"(?!\w)", _canonical(evidence)))

UI_NOISE_LINE = re.compile(
    r"(?:premium subscribers.*|try premium.*|enhance profile|.*\bworks here|"
    r"https?://(?:www\.)?linkedin\.com/jobs/\S*|logout|sign in|notifications?)",
    re.I,
)

FIELD_NOISE = re.compile(
    r"(?:try premium|premium subscribers|enhance profile|works here|linkedin\.com/jobs/|"
    r"^email issue$)",
    re.I,
)

CERTIFICATE_LANGUAGE = re.compile(
    r"\b(certificat(?:e|ion)|credential|issued by|course completion|academy graduate)\b",
    re.I,
)

COURSE_TITLE = re.compile(
    r"\b(?:academy graduate|training\s*:|getting started with|introduction to|"
    r"certificate (?:in|of)|certification|certified\b)", re.I,
)

SKILL_UI = re.compile(
    r"^(?:home|my network|messaging|notifications?|jobs|search|follow|"
    r"tools & technologies|promoted\W*|[a-z]\s+(?:follow|search))$|"
    r"\bfollow\b|\bget the latest jobs\b|\balso follow\b|"
    r"linkedin|©|\bin\s*\(\d+\)", re.I,
)

ACADEMIC_DEGREE = re.compile(
    r"\b(degree|diploma|bachelor(?:'s)?|master(?:'s)?|associate(?:'s)?|doctorate|"
    r"ph\.?\s*d\.?|b\.?\s*(?:a|s|sc|tech|e)\.?|m\.?\s*(?:a|s|sc|tech|e|ba)\.?)\b",
    re.I,
)

POST_CONTEXT = re.compile(r"\b(posts?|activity|published|reposted|comments?)\b", re.I)

PROJECT_CONTEXT = re.compile(
    r"\b(projects?|portfolio|repository|github|demo|built|developed|application|app)\b",
    re.I,
)

DATE_VALUE = re.compile(
    r"^(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+)?"
    r"(?:19|20)\d{2}$|^present$",
    re.I,
)

EDUCATION_LANGUAGE = re.compile(
    r"\b(education|university|college|school|institute|academy|degree|diploma|"
    r"bachelor(?:'s)?|master(?:'s)?|ph\.?\s*d\.?|b\.?\s*(?:a|s|sc|tech|e)\.?|"
    r"m\.?\s*(?:a|s|sc|tech|e|ba)\.?)\b",
    re.I,
)

EDUCATION_SECTION = re.compile(r"\beducation\b", re.I)

EDUCATION_INSTITUTION = re.compile(r"\b(university|college|school|academy)\b", re.I)

EMPLOYMENT_LANGUAGE = re.compile(
    r"\b(experience|employment|employed|employee|company|engineer|developer|"
    r"manager|director|consultant|intern|full[ -]?time|part[ -]?time)\b",
    re.I,
)

def _all_values_supported(item: dict) -> bool:
    evidence = item.get("evidence", "")
    for key, value in item.items():
        if key == "evidence" or not value:
            continue
        values = value if isinstance(value, list) else [value]
        if any(not _value_supported(part, evidence) for part in values):
            return False
    return True

def _education_supported(item: dict, source: str, certifications: list[dict] | None = None) -> bool:
    evidence = item.get("evidence", "")
    institution = item.get("institution")
    if not institution or not _evidence_supported(evidence, source):
        return False
    fields = ("institution", "degree", "field_of_study", "start_date", "end_date")
    if any(not _value_supported(item.get(field), evidence) for field in fields):
        return False
    has_explicit_section = _in_named_section(evidence, source, "education") or bool(
        re.match(r"\s*Education\b", evidence, re.I)
    )
    has_academic_detail = bool(
        (item.get("degree") and ACADEMIC_DEGREE.search(item["degree"]))
    )
    has_school_date_relationship = bool(
        (item.get("start_date") or item.get("end_date"))
        and EDUCATION_INSTITUTION.search(evidence)
    )
    if EMPLOYMENT_LANGUAGE.search(evidence) and not (has_explicit_section or has_academic_detail):
        return False
    if not has_explicit_section and CERTIFICATE_LANGUAGE.search(evidence):
        return False
    for certificate in certifications or []:
        cert_evidence = certificate.get("evidence", "")
        cert_name = certificate.get("name", "")
        cert_issuer = certificate.get("issuer", "")
        if not has_explicit_section and not has_academic_detail and evidence and cert_evidence and (
            _normalize_identity(evidence) in _normalize_identity(cert_evidence)
            or _normalize_identity(cert_evidence) in _normalize_identity(evidence)
        ):
            return False
        institution = _normalize_identity(item.get("institution"))
        if not has_explicit_section and not has_academic_detail and institution and institution in {
            _normalize_identity(cert_name), _normalize_identity(cert_issuer)
        }:
            return False
    return bool(EDUCATION_LANGUAGE.search(evidence)) and (
        has_explicit_section or has_academic_detail or has_school_date_relationship
    )

def _has_content(item: dict) -> bool:
    return any(value for key, value in item.items() if key != "evidence")

def _clean_field(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()

def _in_named_section(evidence: str, source: str, heading: str) -> bool:
    from .sections import HEADINGS, heading_of
    for fields, block in sections(source):
        if heading_of(block.splitlines()[0]) in HEADINGS:
            expected = HEADINGS.get(heading, (heading,))
            if any(field in fields for field in expected if field != "skills") or heading == "skills" and fields == ("skills",):
                if _evidence_supported(evidence, block):
                    return True
    return False

def _skill_supported(item: dict, source: str) -> bool:
    name = _clean_field(item.get("name"))
    evidence = item.get("evidence", "")
    # A skills heading or pipe anywhere in the screenshot is not evidence for
    # every word in that screenshot. Require a local skills line/section, or
    # an exact pipe-delimited skill in the professional headline.
    contextual = any(
        _value_supported(name, line)
        and not re.search(r"tutorial|download|email\.|https?://|www\.", line, re.I)
        and (re.search(r"\b(skills?|expertise|proficient|using)\b", line, re.I)
             or _in_named_section(line, source, "skills"))
        for line in source.splitlines()
    )
    if not contextual:
        from .literal import literal_observation
        contextual = any(_canonical(skill.get("name")) == _canonical(name)
                         for skill in literal_observation(source)["skills"]
                         if "|" in skill.get("evidence", "")
                         and re.search(r"\b(?:intern|engineer|developer|manager|consultant)\b.*(?:@| at )", skill["evidence"], re.I))
    return bool(
        name and len(name) <= 60 and "http" not in name.casefold()
        and re.search(r"[a-zA-Z]", name) and not SKILL_UI.search(name)
        and contextual
        and "|" not in name and "@" not in name
        and not FIELD_NOISE.search(name)
        and not re.search(r"tutorial|\.{2,}|…", name, re.I)
        and _evidence_supported(evidence, source)
        and _value_supported(name, evidence)
    )

def _dates_sane(item: dict) -> bool:
    years = []
    for field in ("start_date", "end_date"):
        value = _clean_field(item.get(field))
        if not value:
            continue
        if not DATE_VALUE.fullmatch(value):
            return False
        match = re.search(r"(?:19|20)\d{2}", value)
        if match:
            years.append((field, int(match.group())))
    by_field = dict(years)
    current_year = timezone.now().year
    if any(year > current_year + 1 for _, year in years):
        return False
    return not (
        by_field.get("start_date") and by_field.get("end_date")
        and by_field["start_date"] > by_field["end_date"]
    )

def _anchor_in_section(item: dict, source: str, field: str) -> bool:
    """Use the entry's identity, not unrelated words elsewhere in a screenshot."""
    from .sections import HEADINGS, heading_of
    title = item.get("title") or item.get("company")
    return bool(title) and any(
        heading_of(block.splitlines()[0]) in HEADINGS and field in fields
        and _value_supported(title, block)
        and _value_supported(item.get("company"), block)
        for fields, block in sections(source)
    )

def _certificate_role(item: dict, source: str) -> bool:
    return bool(COURSE_TITLE.search(item.get("title") or "")) or (
        _anchor_in_section(item, source, "certifications")
        and not _anchor_in_section(item, source, "experiences")
    )

def _experience_supported(item: dict, source: str) -> bool:
    title = _clean_field(item.get("title"))
    company = _clean_field(item.get("company"))
    evidence = item.get("evidence", "")
    explicit_job = _anchor_in_section(item, source, "experiences")
    headline_job = bool(title and company and re.search(
        re.escape(_canonical(title)) + r"\s*(?:@|at\s+)" + re.escape(_canonical(company)),
        _canonical(evidence),
    ))
    return bool(
        (title or company) and len(title) <= 255 and len(company) <= 255
        and (title and company or _in_named_section(evidence, source, "experience"))
        and not any(char in title + company for char in ("|", "@"))
        and not FIELD_NOISE.search(title + " " + company)
        and not _certificate_role(item, source)
        and (explicit_job or headline_job)
        and _evidence_supported(evidence, source)
        and _all_values_supported(item)
    )

def _project_supported(item: dict, source: str) -> bool:
    name = _clean_field(item.get("name"))
    evidence = item.get("evidence", "")
    url = _clean_field(item.get("url"))
    return bool(
        name and not FIELD_NOISE.search(name + " " + (item.get("description") or ""))
        and "linkedin.com/jobs" not in url.casefold()
        and _evidence_supported(evidence, source)
        and _all_values_supported(item)
        and (PROJECT_CONTEXT.search(evidence) or _in_named_section(evidence, source, "projects"))
    )

def _post_supported(item: dict, source: str) -> bool:
    text = _clean_field(item.get("text"))
    evidence = item.get("evidence", "")
    return bool(
        text and not FIELD_NOISE.search(text)
        and _evidence_supported(evidence, source)
        and _all_values_supported(item)
        and (
            POST_CONTEXT.search(evidence)
            or _in_named_section(evidence, source, "posts")
            or _in_named_section(evidence, source, "activity")
        )
    )

def _generic_item_supported(item: dict, source: str) -> bool:
    evidence = item.get("evidence", "")
    return bool(
        item.get("name") and not FIELD_NOISE.search(item.get("name", ""))
        and (CERTIFICATE_LANGUAGE.search(evidence) or COURSE_TITLE.search(item.get("name", ""))
             or _in_named_section(evidence, source, "certifications")
             or _in_named_section(evidence, source, "licenses & certifications"))
        and _evidence_supported(evidence, source) and _all_values_supported(item)
    )

def _sanitize_item(item, source):
    """Drop unsupported fields individually; retain supported parts of the entry."""
    item = dict(item)
    evidence = item.get("evidence") or ""
    if not evidence:
        anchor = next((item.get(k) for k in ("institution", "title", "name", "text", "value", "company") if item.get(k)), None)
        # Recover only a local source block around an exact anchor, never facts
        # elsewhere in the document that happen to share a date/organization.
        if anchor:
            for block in re.split(r"\n\s*\n", source):
                if len(block) <= 1200 and _value_supported(anchor, block):
                    evidence = block
                    break
    if not _evidence_supported(evidence, source):
        return None
    item["evidence"] = evidence
    for key, value in list(item.items()):
        if key == "evidence":
            continue
        if isinstance(value, list):
            item[key] = [v for v in value if _value_supported(v, evidence)]
        elif value and not _value_supported(value, evidence):
            item[key] = None
    return item

def validate_data(data, source, section="AUTO"):
    result = validate_node({"observations": [{"image_id": 0, "source_text": source, "data": data, "section": section}]})
    return result["validated_observations"][0]["data"]

def validate_node(state: ExtractionState):
    validated = []
    for obs in state.get("observations", []):
        source = obs["source_text"]
        section = obs.get("section", "AUTO")
        data = {}
        for key, value in restrict_fields(obs["data"], section).items():
            if isinstance(value, list):
                data[key] = [clean for item in value if (clean := _sanitize_item(item, source))]
            elif isinstance(value, dict):
                data[key] = _sanitize_item(value, source)
        # Only category checks get this context. Evidence above is always
        # checked against the actual OCR, never against an invented heading.
        source = context_source(source, section)
        for item in data.get("experiences", []):
            # A course returned in the wrong model field belongs only in
            # certifications. Never turn its dates into employment dates.
            if _certificate_role(item, source) and item.get("title"):
                data.setdefault("certifications", []).append({
                    "name": item["title"], "issuer": item.get("company"),
                    "evidence": item["evidence"],
                })
            title = item.get("title") or ""
            company = item.get("company") or ""
            if company:
                title = re.sub(r"\s+at\s+" + re.escape(company) + r"$", "", title, flags=re.I)
            # A single OCR icon before a known role is not a different job.
            title = re.sub(r"^[^\w]*[a-z]\s+(?=(?:Intern|Engineer|Developer)\b)", "", title, flags=re.I)
            item["title"] = title or None
            if not _dates_sane(item):
                item["start_date"] = item["end_date"] = None
        for item in data.get("education", []):
            for field in ("start_date", "end_date"):
                if item.get(field) and not DATE_VALUE.fullmatch(item[field]):
                    item[field] = None
            degree = item.get("degree") or ""
            if "," in degree:
                qualification, _, subject = degree.partition(",")
                if not item.get("field_of_study") or subject.strip().casefold() == item["field_of_study"].casefold():
                    item["degree"] = qualification.strip()
                    item["field_of_study"] = subject.strip() or None
        out = {"name": None, "headline": None, "about": None, "location": None,
               "skills": [], "experiences": [], "education": [], "projects": [], "posts": [], "certifications": []}
        for field in ("name", "headline", "about", "location"):
            item = data.get(field)
            if field == "about" and item and not (
                _in_named_section(item.get("evidence", ""), source, "about")
                or _in_named_section(item.get("evidence", ""), source, "summary")
            ):
                continue
            if field == "location" and item and re.search(r"\b(?:intern|engineer|company)\b|@", item.get("evidence", ""), re.I):
                continue
            if field == "location" and item:
                supporting_lines = [line for line in source.splitlines() if _value_supported(item.get("value"), line)]
                if not any(not re.search(r"\b(?:intern|engineer|company)\b|@|\|", line, re.I) for line in supporting_lines):
                    continue
            if item and item.get("value") and _value_supported(item.get("value"), item.get("evidence", "")) and _evidence_supported(item.get("evidence", ""), source):
                out[field] = item
        out["skills"] = [item for item in data.get("skills", []) if _skill_supported(item, source)]
        out["experiences"] = [
            item for item in data.get("experiences", []) if _experience_supported(item, source)
        ]
        out["projects"] = [item for item in data.get("projects", []) if _project_supported(item, source)]
        out["posts"] = [item for item in data.get("posts", []) if _post_supported(item, source)]
        out["certifications"] = [
            item for item in data.get("certifications", []) if _generic_item_supported(item, source)
        ]
        for item in data.get("education", []):
            if _education_supported(item, source, out["certifications"]):
                out["education"].append(item)
        validated.append({"image_id": obs["image_id"], "data": restrict_fields(out, section)})
    return {"validated_observations": validated}

def _normalize_identity(value):
    # Ignore OCR separators, but do not conflate meaningful names such as
    # C++ and C# projects or employers.
    return re.sub(r"(?:[^\w+#/&]|_)+", " ", _canonical(value)).strip()

def _degree_identity(value):
    value = _normalize_identity(value)
    # Redundant abbreviation suffixes do not make a separate qualification.
    # Only known exact pairs are collapsed; different degrees/subjects remain.
    aliases = {
        "bachelor of engineering": ("be", "b e"),
        "bachelor of science": ("bsc", "b sc", "bs", "b s"),
        "master of science": ("msc", "m sc", "ms", "m s"),
        "bachelor of technology": ("btech", "b tech"),
        "master of technology": ("mtech", "m tech"),
    }
    for full, abbreviations in aliases.items():
        if value in {full, *(full + " " + suffix for suffix in abbreviations)}:
            return full
    return value

def _records_match(left, right, identity_fields):
    # A shared date alone is never a person's school/job/project identity.
    anchors = [key for key in identity_fields if key not in {"start_date", "end_date"}]
    if not any(left.get(key) and right.get(key) for key in anchors):
        return False
    compared = False
    for field in identity_fields:
        a, b = _normalize_identity(left.get(field)), _normalize_identity(right.get(field))
        if field == "degree":
            a, b = _degree_identity(left.get(field)), _degree_identity(right.get(field))
        if a and b:
            compared = True
            if field in {"start_date", "end_date", "issue_date"}:
                # A year-only crop and a month/year crop can describe the same
                # date. Two different known months/years must remain distinct.
                if re.fullmatch(r"(?:19|20)\d{2}", a) and b.endswith(a):
                    continue
                if re.fullmatch(r"(?:19|20)\d{2}", b) and a.endswith(b):
                    continue
            if a != b:
                return False
    return compared

def _merge_records(items, identity_fields):
    out = []
    for item in items:
        existing = next((row for row in out if _records_match(row, item, identity_fields)), None)
        if existing is None:
            out.append(dict(item))
            continue
        for key, value in item.items():
            if not value:
                continue
            if isinstance(value, list):
                existing[key] = list(dict.fromkeys([*(existing.get(key) or []), *value]))
            elif not existing.get(key) or (key in {"description", "text"} and len(str(value)) > len(str(existing[key]))):
                existing[key] = value
            elif key in {"start_date", "end_date", "issue_date"} and len(str(value)) > len(str(existing[key])):
                existing[key] = value
    return out

def merge_node(state: ExtractionState):
    merged = {"name": None, "headline": None, "about": None, "location": None,
              "skills": [], "experiences": [], "education": [], "projects": [], "posts": [], "certifications": [], "evidence": []}

    for obs in state.get("validated_observations", []):
        image_id = obs["image_id"]
        data = obs["data"]
        for field in ("name", "headline", "location"):
            item = data.get(field)
            if item and not merged[field]:
                merged[field] = item["value"]
                merged["evidence"].append((image_id, field, item["value"], item["evidence"]))
        item = data.get("about")
        if item and (not merged["about"] or len(item["value"]) > len(merged["about"])):
            merged["about"] = item["value"]
            merged["evidence"].append((image_id, "about", item["value"], item["evidence"]))

        for skill in data.get("skills", []):
            merged["skills"].append(skill["name"])
            merged["evidence"].append((image_id, "skill", skill["name"], skill["evidence"]))

        for field in ("experiences", "education", "projects", "posts", "certifications"):
            for item in data.get(field, []):
                clean = {k:v for k,v in item.items() if k != "evidence"}
                merged[field].append(clean)
                label = next((str(v) for k,v in clean.items() if v and not isinstance(v, list)), field)
                merged["evidence"].append((image_id, field.rstrip("s"), label, item["evidence"]))

    unique_skills = {}
    for skill in merged["skills"]:
        clean_skill = skill.strip() if skill else ""
        if clean_skill:
            unique_skills.setdefault(clean_skill.casefold(), clean_skill)
    merged["skills"] = list(unique_skills.values())
    merged["experiences"] = _merge_records(
        merged["experiences"],
        ("title", "company", "start_date", "end_date"),
    )
    merged["education"] = _merge_records(
        merged["education"],
        ("institution", "degree", "field_of_study", "start_date", "end_date"),
    )
    merged["projects"] = _merge_records(merged["projects"], ("name", "url"))
    merged["posts"] = _merge_records(merged["posts"], ("text",))
    merged["certifications"] = _merge_records(merged["certifications"], ("name", "issuer", "credential_id", "issue_date"))
    return {"merged_profile": merged}
