
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

UNIVERSITY_ALIASES = {
    "cmu": "CMU",
    "carnegie mellon": "CMU",
    "mit": "MIT",
    "wright state": "Wright State",
    "msu": "Michigan State",
    "michigan state": "Michigan State",
    "uw": "University of Washington",
    "university of washington": "University of Washington",
    "rutgers": "Rutgers",
    "sdsu": "San Diego State",
    "franklin": "Franklin University",
    "franklin university": "Franklin University",
}

IMPORTANT_MEMORY_KEYWORDS = [
    "gpa", "budget", "cmu", "mit", "wright state", "funding",
    "sop", "gre", "toefl", "ielts", "research", "work experience",
    "github", "linkedin", "ai", "ml", "data science", "cybersecurity",
    "software engineering", "deadline", "scholarship",
]


def update_memory(ctx: Dict[str, Any], user_message: str, reply: str) -> None:
    """Ports StudentAgent.update_memory verbatim: tracks important points and
    university-alias mentions in the durable per-student memory dict."""
    memory = ctx["memory"]
    text = user_message.lower()

    if any(keyword in text for keyword in IMPORTANT_MEMORY_KEYWORDS):
        point = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "user": user_message,
            "aria": (reply or "")[:500],
        }

        memory["important_points"].append(point)
        memory["important_points"] = memory["important_points"][-50:]

    for alias, canonical_name in UNIVERSITY_ALIASES.items():
        if alias in text and canonical_name not in memory["universities_discussed"]:
            memory["universities_discussed"].append(canonical_name)
