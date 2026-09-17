# profile_intelligence/profile_intelligence.py

import json
import os
from datetime import datetime
from typing import Dict

from profile_intelligence.github_analyzer import GitHubAnalyzer
from profile_intelligence.course_mapper import map_interests_to_courses


class ProfileIntelligenceService:
    """
    Main service used by Aria or CLI.

    It analyzes a student's GitHub profile and returns:
    - technical interests
    - evidence
    - recommended course tracks
    - advisor-style note
    """

    def __init__(self, storage_dir: str = "data/profile_intelligence"):
        self.storage_dir = storage_dir
        os.makedirs(storage_dir, exist_ok=True)
        self.github_analyzer = GitHubAnalyzer()

    def analyze_github(self, github_input: str, student_name: str = "student") -> Dict:
        github_analysis = self.github_analyzer.analyze(github_input)

        course_recommendation = map_interests_to_courses(
            github_analysis["inferred_interests"],
            top_n=3
        )

        result = {
            "student_name": student_name,
            "generated_at": datetime.now().isoformat(),
            "github_analysis": github_analysis,
            "course_recommendation": course_recommendation,
            "human_summary": self._build_human_summary(
                github_analysis,
                course_recommendation
            )
        }

        self.save_result(student_name, result)

        return result

    def save_result(self, student_name: str, result: Dict):
        safe_name = student_name.lower().replace(" ", "_")
        path = os.path.join(self.storage_dir, f"{safe_name}_github_analysis.json")

        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

    def _build_human_summary(self, github_analysis: Dict, course_recommendation: Dict) -> str:
        profile = github_analysis.get("profile", {})
        top_languages = github_analysis.get("top_languages", [])
        ranked_interests = github_analysis.get("inferred_interests", {}).get("ranked_interests", [])

        top_language_names = [lang for lang, _ in top_languages[:3]]
        top_interest_names = [
            interest for interest, score in ranked_interests[:3]
            if score > 0
        ]

        primary_direction = course_recommendation.get("primary_direction", "Unclear")

        if primary_direction == "Unclear":
            return (
                "I could not infer a strong academic direction from this GitHub profile alone. "
                "The student should share a resume, LinkedIn summary, or explain private/college projects."
            )

        return (
            f"The GitHub profile suggests a primary direction toward {primary_direction}. "
            f"Top visible languages include {', '.join(top_language_names) if top_language_names else 'not enough visible data'}. "
            f"The strongest interest signals are {', '.join(top_interest_names) if top_interest_names else 'not clearly visible'}. "
            f"{course_recommendation.get('advisor_note')}"
        )