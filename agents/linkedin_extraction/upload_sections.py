"""User-selected routing is an output boundary, not evidence for new facts."""
from .sections import SCALARS

SECTION_FIELDS = {
    "PROFILE": SCALARS + ("skills",),
    "EXPERIENCE": ("experiences",),
    "EDUCATION": ("education",),
    "CERTIFICATES": ("certifications",),
}
SECTION_HEADINGS = {"EXPERIENCE": "Experience", "EDUCATION": "Education", "CERTIFICATES": "Certifications"}


def restrict_fields(data, section):
    allowed = SECTION_FIELDS.get(section)
    return {k: v for k, v in data.items() if allowed is None or k in allowed}


def context_source(source, section):
    heading = SECTION_HEADINGS.get(section)
    return heading + "\n" + source if heading else source
