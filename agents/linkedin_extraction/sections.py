"""Keep extraction requests small without truncating the uploaded source."""
import re

SCALARS = ("name", "headline", "about", "location")
COLLECTIONS = ("skills", "experiences", "education", "projects", "posts", "certifications")
HEADINGS = {
    "about": ("about",), "summary": ("about",),
    "skills": ("skills",), "technical skills": ("skills",),
    "experience": ("experiences", "skills"), "work experience": ("experiences", "skills"),
    "education": ("education", "skills"), "projects": ("projects", "skills"),
    "posts": ("posts",), "activity": ("posts",),
    "certifications": ("certifications",),
    "licenses & certifications": ("certifications",),
    "licenses and certifications": ("certifications",),
}
STOP_HEADINGS = {"connected apps", "people also viewed", "who your viewers also viewed",
                 "profile language", "public profile & url", "promoted",
                 "people you may know", "you might like"}


def heading_of(line):
    value = line.strip().casefold()
    for heading in sorted((*HEADINGS, *STOP_HEADINGS), key=len, reverse=True):
        if re.fullmatch(re.escape(heading) + r"[\s:+@©®0-9()\-oo]*", value):
            return heading
    return re.sub(r"[^a-z& ]", "", value).strip()


def sections(source):
    fields, lines = SCALARS + COLLECTIONS, []
    for line in source.splitlines():
        heading = heading_of(line)
        if heading in HEADINGS or heading in STOP_HEADINGS:
            if lines and fields:
                yield fields, "\n".join(lines)
            fields = HEADINGS.get(heading, ())
            lines = [line]
        else:
            lines.append(line)
    if lines and fields:
        yield fields, "\n".join(lines)


def chunks(source, limit=2200, overlap=350):
    """Cover every character, preferring line boundaries with overlap."""
    start = 0
    while start < len(source):
        end = min(len(source), start + limit)
        if end < len(source):
            boundary = source.rfind("\n", start + limit // 2, end)
            if boundary > start:
                end = boundary
        yield source[start:end]
        if end == len(source):
            break
        start = max(start + 1, end - overlap)


def extraction_tasks(source):
    for fields, text in sections(source):
        for chunk in chunks(text):
            if chunk.strip():
                # Small local models omit scalar fields when asked to generate
                # every collection at once. Extract each family independently.
                if fields == SCALARS + COLLECTIONS:
                    yield SCALARS, chunk
                    yield ("skills",), chunk
                    if re.search(r"\b(intern|engineer|developer|manager|experience|worked|employment)\b", chunk, re.I):
                        yield ("experiences",), chunk
                    if re.search(r"\b(education|bachelor|master|diploma|degree|bsc|msc|phd)\b", chunk, re.I):
                        yield ("education",), chunk
                    if re.search(r"\b(project|projects|built|developed|portfolio)\b", chunk, re.I):
                        yield ("projects",), chunk
                    if re.search(r"\b(posts?|activity|reposted)\b", chunk, re.I):
                        yield ("posts",), chunk
                    if re.search(r"\b(certificat\w*|credential|academy graduate|training\s*:|getting started with|introduction to)\b", chunk, re.I):
                        yield ("certifications",), chunk
                else:
                    # Preserve the category of continuation chunks. Evidence
                    # still has to match the original source on final validation.
                    heading = text.splitlines()[0]
                    if not chunk.startswith(heading):
                        chunk = heading + "\n" + chunk
                    yield fields, chunk
