# profile_intelligence/course_mapper.py

from typing import Dict, List


COURSE_TRACK_MAP = {
    "AI/ML": [
        "MS Computer Science with AI/ML specialization",
        "MS Artificial Intelligence",
        "MS Data Science with Machine Learning focus",
        "MS Computer Science with NLP / Computer Vision electives",
    ],
    "Data Science / Analytics": [
        "MS Data Science",
        "MS Business Analytics",
        "MS Applied Data Analytics",
        "MS Computer Science with Data Mining / Big Data electives",
    ],
    "Backend Engineering": [
        "MS Computer Science with Software Systems focus",
        "MS Software Engineering",
        "MS Computer Science with Distributed Systems / Cloud electives",
    ],
    "Frontend / Full Stack": [
        "MS Software Engineering",
        "MS Computer Science with Human-Computer Interaction electives",
        "MS Information Systems",
    ],
    "Cybersecurity": [
        "MS Cybersecurity",
        "MS Information Security",
        "MS Computer Science with Security specialization",
    ],
    "Cloud / DevOps": [
        "MS Computer Science with Cloud Computing focus",
        "MS Software Engineering",
        "MS Information Systems with Cloud / DevOps electives",
    ],
    "Software Engineering": [
        "MS Software Engineering",
        "MS Computer Science with Software Engineering focus",
        "MS Information Systems",
    ],
    "HCI / UX": [
        "MS Human-Computer Interaction",
        "MS Information Science",
        "MS Computer Science with HCI electives",
    ],
    "Health Informatics": [
        "MS Health Informatics",
        "MS Biomedical Informatics",
        "MS Data Science with Healthcare Analytics focus",
    ],
}


def map_interests_to_courses(interest_analysis: Dict, top_n: int = 3) -> Dict:
    """
    Maps inferred GitHub interests to recommended academic tracks.
    """
    ranked_interests = interest_analysis.get("ranked_interests", [])

    selected = [
        (interest, score)
        for interest, score in ranked_interests
        if score > 0
    ][:top_n]

    recommendations = []

    for interest, score in selected:
        tracks = COURSE_TRACK_MAP.get(interest, [])

        recommendations.append({
            "interest_area": interest,
            "score": score,
            "recommended_tracks": tracks,
            "reason": _reason_for_interest(interest),
        })

    if not recommendations:
        return {
            "primary_direction": "Unclear",
            "recommendations": [],
            "advisor_note": (
                "The GitHub profile does not show enough clear technical signals yet. "
                "The student may need to share resume, LinkedIn summary, or private project details."
            )
        }

    primary_direction = recommendations[0]["interest_area"]

    return {
        "primary_direction": primary_direction,
        "recommendations": recommendations,
        "advisor_note": build_advisor_note(recommendations),
    }


def _reason_for_interest(interest: str) -> str:
    reasons = {
        "AI/ML": (
            "The profile shows signals related to Python, ML projects, NLP, deep learning, "
            "or data-driven experimentation."
        ),
        "Data Science / Analytics": (
            "The profile shows signals around data handling, notebooks, SQL, dashboards, "
            "analytics, or visualization."
        ),
        "Backend Engineering": (
            "The profile shows API, backend, server-side, database, or framework-oriented work."
        ),
        "Frontend / Full Stack": (
            "The profile shows web development, UI, JavaScript/TypeScript, React, or full-stack work."
        ),
        "Cybersecurity": (
            "The profile shows security, encryption, vulnerability, CTF, or cyber-related work."
        ),
        "Cloud / DevOps": (
            "The profile shows Docker, cloud, CI/CD, deployment, infrastructure, or DevOps signals."
        ),
        "Software Engineering": (
            "The profile shows general software development patterns and engineering-oriented projects."
        ),
        "HCI / UX": (
            "The profile shows UI/UX, user experience, HCI, prototyping, or design-facing projects."
        ),
        "Health Informatics": (
            "The profile shows healthcare, medical, clinical, bioinformatics, or health-data signals."
        ),
    }

    return reasons.get(interest, "The profile shows relevant technical signals.")


def build_advisor_note(recommendations: List[Dict]) -> str:
    """
    Creates a natural advisor-style summary.
    """
    if not recommendations:
        return (
            "I do not see a clear direction from the GitHub profile alone. "
            "I would ask the student for a resume or project explanation before recommending a track."
        )

    primary = recommendations[0]["interest_area"]

    if len(recommendations) == 1:
        return (
            f"The GitHub profile mainly points toward {primary}. "
            f"I would recommend programs or electives aligned with that direction."
        )

    secondary = recommendations[1]["interest_area"]

    return (
        f"The GitHub profile mainly points toward {primary}, with secondary signals around "
        f"{secondary}. I would recommend course tracks that combine these areas rather than "
        f"choosing a generic program blindly."
    )