# profile_intelligence/github_analyzer.py

import os
import re
import requests
from collections import Counter
from typing import Dict, List, Optional


GITHUB_API_BASE = "https://api.github.com"


def extract_github_username(github_input: str) -> str:
    """
    Accepts:
    - akshay123
    - https://github.com/akshay123
    - github.com/akshay123

    Returns:
    - akshay123
    """
    github_input = github_input.strip()

    github_input = github_input.replace("https://", "").replace("http://", "")
    github_input = github_input.replace("www.", "")

    if github_input.startswith("github.com/"):
        parts = github_input.split("/")
        if len(parts) >= 2:
            return parts[1].strip()

    return github_input.strip("/")


class GitHubAnalyzer:
    """
    Fetches and analyzes public GitHub profile data.

    It does not require authentication for basic usage.
    Optional:
    Add GITHUB_TOKEN in .env to increase GitHub API rate limit.
    """

    def __init__(self):
        self.github_token = os.getenv("GITHUB_TOKEN")

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Kormic-Profile-Analyzer"
        }

        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"

        return headers

    def _get(self, url: str, params: Optional[Dict] = None):
        response = requests.get(
            url,
            headers=self._headers(),
            params=params,
            timeout=20
        )

        if response.status_code == 404:
            raise ValueError("GitHub profile not found.")

        if response.status_code == 403:
            raise ValueError(
                "GitHub API rate limit reached. Add GITHUB_TOKEN in .env or try later."
            )

        response.raise_for_status()
        return response.json()

    def fetch_user(self, username: str) -> Dict:
        return self._get(f"{GITHUB_API_BASE}/users/{username}")

    def fetch_repos(self, username: str, max_repos: int = 30) -> List[Dict]:
        repos = self._get(
            f"{GITHUB_API_BASE}/users/{username}/repos",
            params={
                "sort": "updated",
                "direction": "desc",
                "per_page": max_repos
            }
        )

        # Ignore forks mostly, because we want student's own work.
        own_repos = [repo for repo in repos if not repo.get("fork")]
        return own_repos[:max_repos]

    def fetch_repo_languages(self, repo_full_name: str) -> Dict[str, int]:
        try:
            return self._get(f"{GITHUB_API_BASE}/repos/{repo_full_name}/languages")
        except Exception:
            return {}

    def fetch_readme_text(self, repo_full_name: str) -> str:
        """
        Uses GitHub's raw README endpoint through API response.
        If README is unavailable, returns empty string.
        """
        try:
            response = requests.get(
                f"https://raw.githubusercontent.com/{repo_full_name}/HEAD/README.md",
                headers={"User-Agent": "Kormic-Profile-Analyzer"},
                timeout=15
            )

            if response.status_code == 200:
                return response.text[:3000]

            # fallback for master branch
            response = requests.get(
                f"https://raw.githubusercontent.com/{repo_full_name}/master/README.md",
                headers={"User-Agent": "Kormic-Profile-Analyzer"},
                timeout=15
            )

            if response.status_code == 200:
                return response.text[:3000]

            # fallback for main branch
            response = requests.get(
                f"https://raw.githubusercontent.com/{repo_full_name}/main/README.md",
                headers={"User-Agent": "Kormic-Profile-Analyzer"},
                timeout=15
            )

            if response.status_code == 200:
                return response.text[:3000]

            return ""

        except Exception:
            return ""

    def analyze(self, github_input: str, max_repos: int = 20) -> Dict:
        username = extract_github_username(github_input)

        user = self.fetch_user(username)
        repos = self.fetch_repos(username, max_repos=max_repos)

        language_counter = Counter()
        topic_counter = Counter()
        keyword_counter = Counter()
        analyzed_repos = []

        for repo in repos:
            full_name = repo.get("full_name")
            repo_name = repo.get("name", "")
            description = repo.get("description") or ""
            topics = repo.get("topics", []) or []

            languages = self.fetch_repo_languages(full_name)
            readme = self.fetch_readme_text(full_name)

            for lang, count in languages.items():
                language_counter[lang] += count

            for topic in topics:
                topic_counter[topic.lower()] += 1

            combined_text = f"{repo_name} {description} {' '.join(topics)} {readme}"
            keywords = self._extract_keywords(combined_text)

            for keyword in keywords:
                keyword_counter[keyword] += 1

            analyzed_repos.append({
                "name": repo_name,
                "full_name": full_name,
                "description": description,
                "url": repo.get("html_url"),
                "topics": topics,
                "languages": languages,
                "stars": repo.get("stargazers_count", 0),
                "updated_at": repo.get("updated_at"),
                "keywords": keywords[:20],
            })

        interests = self._infer_interests(
            languages=language_counter,
            topics=topic_counter,
            keywords=keyword_counter
        )

        return {
            "username": username,
            "profile": {
                "name": user.get("name"),
                "bio": user.get("bio"),
                "company": user.get("company"),
                "location": user.get("location"),
                "blog": user.get("blog"),
                "public_repos": user.get("public_repos"),
                "followers": user.get("followers"),
                "profile_url": user.get("html_url"),
            },
            "top_languages": language_counter.most_common(10),
            "top_topics": topic_counter.most_common(20),
            "top_keywords": keyword_counter.most_common(30),
            "repos_analyzed": analyzed_repos,
            "inferred_interests": interests,
        }

    def _extract_keywords(self, text: str) -> List[str]:
        """
        Simple keyword extraction using known tech/domain words.
        Later we can replace this with LLM-based extraction.
        """
        text = text.lower()

        keyword_bank = [
            # AI / ML
            "machine learning", "deep learning", "artificial intelligence",
            "ai", "ml", "nlp", "bert", "transformer", "llm",
            "computer vision", "opencv", "tensorflow", "pytorch",
            "scikit-learn", "classification", "regression", "prediction",

            # Data
            "data science", "data analytics", "pandas", "numpy",
            "matplotlib", "power bi", "tableau", "dashboard",
            "visualization", "etl", "sql", "mysql", "postgresql",

            # Backend
            "api", "rest api", "backend", "fastapi", "flask",
            "django", "node", "express", "spring boot",

            # Frontend
            "frontend", "react", "vue", "angular", "javascript",
            "typescript", "html", "css", "tailwind",

            # Cloud / DevOps
            "docker", "kubernetes", "aws", "azure", "gcp",
            "ci/cd", "github actions", "terraform", "devops",

            # Security
            "cybersecurity", "security", "encryption", "malware",
            "vulnerability", "penetration testing", "ctf",

            # Software engineering
            "software engineering", "microservices", "distributed system",
            "system design", "testing", "automation",

            # HCI / UX
            "ui", "ux", "hci", "user experience", "figma",

            # Healthcare / bio
            "healthcare", "medical", "bioinformatics", "clinical",
        ]

        found = []

        for keyword in keyword_bank:
            if keyword in text:
                found.append(keyword)

        # Also collect useful plain words from repo text
        tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9\-\+\.#]{2,}", text)
        stop_words = {
            "the", "and", "for", "with", "this", "that", "from", "into",
            "using", "used", "project", "application", "system", "simple",
            "basic", "github", "readme", "install", "usage"
        }

        useful_tokens = [
            token for token in tokens
            if token not in stop_words and len(token) <= 25
        ]

        found.extend(useful_tokens[:40])

        return list(dict.fromkeys(found))

    def _infer_interests(
        self,
        languages: Counter,
        topics: Counter,
        keywords: Counter
    ) -> Dict:
        """
        Rule-based first version.
        Gives domain scores and evidence.
        """
        scores = {
            "AI/ML": 0,
            "Data Science / Analytics": 0,
            "Backend Engineering": 0,
            "Frontend / Full Stack": 0,
            "Cybersecurity": 0,
            "Cloud / DevOps": 0,
            "Software Engineering": 0,
            "HCI / UX": 0,
            "Health Informatics": 0,
        }

        evidence = {key: [] for key in scores}

        def add(domain: str, points: int, reason: str):
            scores[domain] += points
            evidence[domain].append(reason)

        language_names = {lang.lower(): count for lang, count in languages.items()}
        topic_names = dict(topics)
        keyword_names = dict(keywords)

        # Languages
        if "python" in language_names:
            add("AI/ML", 3, "Python is heavily used.")
            add("Data Science / Analytics", 3, "Python is heavily used.")

        if "jupyter notebook" in language_names:
            add("Data Science / Analytics", 4, "Jupyter notebooks are present.")
            add("AI/ML", 2, "Jupyter notebooks often indicate ML/data experiments.")

        if "javascript" in language_names or "typescript" in language_names:
            add("Frontend / Full Stack", 3, "JavaScript/TypeScript is used.")

        if "java" in language_names:
            add("Software Engineering", 2, "Java is used.")
            add("Backend Engineering", 2, "Java often indicates backend/software engineering.")

        if "html" in language_names or "css" in language_names:
            add("Frontend / Full Stack", 2, "HTML/CSS are used.")

        # Topics + keywords
        all_terms = set(topic_names.keys()) | set(keyword_names.keys())

        ai_terms = {
            "machine learning", "deep learning", "artificial intelligence",
            "ai", "ml", "nlp", "bert", "transformer", "llm",
            "computer vision", "opencv", "tensorflow", "pytorch",
            "scikit-learn", "classification", "regression", "prediction"
        }

        data_terms = {
            "data science", "data analytics", "pandas", "numpy",
            "matplotlib", "power bi", "tableau", "dashboard",
            "visualization", "etl", "sql", "mysql", "postgresql"
        }

        backend_terms = {
            "api", "rest api", "backend", "fastapi", "flask",
            "django", "node", "express", "spring boot", "microservices"
        }

        frontend_terms = {
            "frontend", "react", "vue", "angular", "javascript",
            "typescript", "tailwind", "ui"
        }

        cloud_terms = {
            "docker", "kubernetes", "aws", "azure", "gcp",
            "ci/cd", "github actions", "terraform", "devops"
        }

        security_terms = {
            "cybersecurity", "security", "encryption", "malware",
            "vulnerability", "penetration testing", "ctf"
        }

        hci_terms = {
            "ux", "hci", "user experience", "figma"
        }

        health_terms = {
            "healthcare", "medical", "bioinformatics", "clinical"
        }

        for term in all_terms:
            if term in ai_terms:
                add("AI/ML", 4, f"Found AI/ML signal: {term}")

            if term in data_terms:
                add("Data Science / Analytics", 3, f"Found data signal: {term}")

            if term in backend_terms:
                add("Backend Engineering", 3, f"Found backend signal: {term}")

            if term in frontend_terms:
                add("Frontend / Full Stack", 3, f"Found frontend/full-stack signal: {term}")

            if term in cloud_terms:
                add("Cloud / DevOps", 3, f"Found cloud/devops signal: {term}")

            if term in security_terms:
                add("Cybersecurity", 4, f"Found security signal: {term}")

            if term in hci_terms:
                add("HCI / UX", 3, f"Found HCI/UX signal: {term}")

            if term in health_terms:
                add("Health Informatics", 4, f"Found healthcare/bio signal: {term}")

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        return {
            "scores": scores,
            "ranked_interests": ranked,
            "evidence": evidence,
        }