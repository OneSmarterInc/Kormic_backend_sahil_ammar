"""The admissions context exchanged with a university agent.

Keep canonical identity outside model-editable fields. Do not transmit emails,
raw documents, private notes, other universities' assessments, or chat memory.
"""
from copy import deepcopy


ADMISSIONS_FIELDS = (
    "name", "country", "institution", "major", "program", "disciplines",
    "gpa", "gpa_scale", "gre_quant", "gre_verbal", "toefl", "ielts",
    "english_score_text", "budget", "budget_text", "graduation_year",
    "work_months", "work_experience_summary", "research", "research_interests",
    "skills", "projects", "publications_count", "career_goals", "preferences",
)


def university_context(student_id, profile):
    return {
        "student_id": str(student_id),
        **{key: deepcopy(profile[key]) for key in ADMISSIONS_FIELDS if key in profile},
    }
