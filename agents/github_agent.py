# agents/github_agent.py
# GitHub Skills Agent for the Kormic Commons.
# Reads a student's public GitHub profile and evaluates demonstrated skills.

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import anthropic
import requests
from rich.console import Console

console = Console()

MODEL = "claude-haiku-4-5-20251001"


# Bounds the assessment-synthesis call so a hung upstream request can't
# hold the request worker indefinitely (see GitHubAnalyzeAPIView).
ANTHROPIC_CLIENT_TIMEOUT_SECONDS = 120.0


def _get_anthropic_client() -> anthropic.Anthropic:
    """
    Create Anthropic client only when assessment synthesis is required.

    This keeps imports safe even before ANTHROPIC_API_KEY is configured.
    If the key is missing, the agent will still return a rule-based fallback.
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not found. Falling back to rule-based GitHub assessment."
        )

    return anthropic.Anthropic(timeout=ANTHROPIC_CLIENT_TIMEOUT_SECONDS, max_retries=1)


class GitHubSkillsAgent:
    """
    Evaluates a student's demonstrated skills from their public GitHub profile.

    This agent is different from the course recommendation mapper:
    - GitHubSkillsAgent asks: "What has this student actually demonstrated?"
    - Course mapper asks: "Which academic track fits those interests?"

    Output is a structured skills assessment that Aria can use as verified
    evidence alongside self-reported resume/profile information.
    """

    def __init__(self, token: Optional[str] = None):
        # Prefer a caller-supplied token (e.g. a student's own connected
        # GitHub OAuth token) so their scans count against their personal
        # rate limit; fall back to the shared server-side token otherwise.
        token = token or os.getenv("GITHUB_TOKEN")

        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Kormic-GitHub-Skills-Agent",
        }

        if token:
            # GitHub accepts this format for classic personal access tokens.
            self.headers["Authorization"] = f"token {token}"

        self.base = "https://api.github.com"

    # ------------------------------------------------------------------
    # GitHub API helpers
    # ------------------------------------------------------------------

    def _get_response(
        self,
        endpoint: str,
        timeout: int = 15,
    ) -> Optional[requests.Response]:
        """Make a GitHub API request and return the raw response."""
        try:
            response = requests.get(
                f"{self.base}{endpoint}",
                headers=self.headers,
                timeout=timeout,
            )

            if response.status_code in (200, 201):
                return response

            if response.status_code == 404:
                return None

            if response.status_code == 403:
                rate_remaining = response.headers.get("X-RateLimit-Remaining")
                rate_reset = response.headers.get("X-RateLimit-Reset")

                msg = f"[yellow]GitHub API 403 for {endpoint}."

                if rate_remaining == "0":
                    msg += " Rate limit appears exhausted."
                    if rate_reset:
                        msg += f" Reset timestamp: {rate_reset}."
                else:
                    msg += " You may lack access or have hit a secondary rate limit."

                msg += "[/yellow]"
                console.print(msg)
                return None

            console.print(
                f"[yellow]GitHub API {response.status_code}: {endpoint}[/yellow]"
            )
            return None

        except requests.RequestException as exc:
            console.print(f"[red]GitHub request failed for {endpoint}: {exc}[/red]")
            return None

    def _get(self, endpoint: str) -> Optional[Any]:
        response = self._get_response(endpoint)

        if response is None:
            return None

        try:
            return response.json()
        except Exception as exc:
            console.print(f"[yellow]Could not parse GitHub JSON for {endpoint}: {exc}[/yellow]")
            return None

    def _extract_username(self, github_url_or_username: str) -> str:
        """Accept a GitHub URL or username and return a clean username."""
        value = str(github_url_or_username or "").strip().strip("/")

        if not value:
            raise ValueError("GitHub username is empty.")

        value = value.replace("https://", "").replace("http://", "")
        value = value.replace("www.", "")

        if value.startswith("github.com/"):
            value = value.split("github.com/", 1)[1]

        username = value.split("/", 1)[0].strip()

        if not username:
            raise ValueError("GitHub username is empty.")

        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", username):
            raise ValueError(f"Invalid GitHub username: {username}")

        return username

    def _fetch_profile(self, username: str) -> Optional[dict]:
        profile = self._get(f"/users/{username}")
        return profile if isinstance(profile, dict) else None

    def _fetch_repos(self, username: str) -> List[dict]:
        repos = self._get(f"/users/{username}/repos?per_page=100&sort=updated")
        return repos if isinstance(repos, list) else []

    def _fetch_languages(self, owner: str, repo_name: str) -> Dict[str, int]:
        languages = self._get(f"/repos/{owner}/{repo_name}/languages")
        return languages if isinstance(languages, dict) else {}

    def _fetch_readme(self, owner: str, repo_name: str) -> str:
        readme = self._get(f"/repos/{owner}/{repo_name}/readme")

        if not readme or not isinstance(readme, dict) or not readme.get("content"):
            return ""

        try:
            decoded = base64.b64decode(readme["content"]).decode(
                "utf-8",
                errors="ignore",
            )
            return decoded[:1500]
        except Exception:
            return ""

    def _fetch_tree_files(
        self,
        owner: str,
        repo_name: str,
        default_branch: str,
    ) -> List[str]:
        """Fetch repo file tree for lightweight project-quality signals."""
        if not default_branch:
            return []

        tree = self._get(
            f"/repos/{owner}/{repo_name}/git/trees/{default_branch}?recursive=1"
        )

        if not tree or not isinstance(tree, dict):
            return []

        files: List[str] = []

        for item in tree.get("tree", [])[:2000]:
            if item.get("type") == "blob" and item.get("path"):
                files.append(item["path"])

        return files

    def _fetch_commit_count_and_dates(
        self,
        owner: str,
        repo_name: str,
        username: str,
    ) -> Tuple[int, List[str]]:
        """
        Fetch approximate commit count for the user in a repo.

        GitHub exposes exact count through pagination Link header in many cases.
        If Link is unavailable, use the number of commits returned on the first page.
        """
        endpoint = f"/repos/{owner}/{repo_name}/commits?per_page=100&author={username}"
        response = self._get_response(endpoint)

        if response is None:
            return 0, []

        try:
            commits = response.json()
        except Exception:
            return 0, []

        if not isinstance(commits, list):
            return 0, []

        commit_dates: List[str] = []

        for commit in commits:
            date = (
                commit.get("commit", {})
                .get("author", {})
                .get("date")
            )
            if date:
                commit_dates.append(date)

        link_header = response.headers.get("Link", "")
        last_page = self._parse_last_page_from_link_header(link_header)

        if last_page:
            # per_page=100, so exact count is not guaranteed,
            # but this is better than always returning one page.
            return max(len(commits), (last_page - 1) * 100 + len(commits)), commit_dates

        return len(commits), commit_dates

    def _parse_last_page_from_link_header(self, link_header: str) -> Optional[int]:
        if not link_header:
            return None

        match = re.search(r"[?&]page=(\d+)>; rel=\"last\"", link_header)

        if not match:
            return None

        try:
            return int(match.group(1))
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Signal extraction
    # ------------------------------------------------------------------

    def _months_active(self, repos: List[dict], commit_dates: List[str]) -> int:
        dates = []

        for repo in repos:
            for key in ["created_at", "pushed_at", "updated_at"]:
                value = repo.get(key)
                if value:
                    try:
                        dates.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
                    except Exception:
                        pass

        for value in commit_dates:
            try:
                dates.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except Exception:
                pass

        if not dates:
            return 0

        earliest = min(dates)
        now = datetime.now(timezone.utc)

        return max(1, (now - earliest).days // 30)

    def _repo_quality_signals(self, files: List[str], readme: str) -> Dict[str, Any]:
        lower_files = [file.lower() for file in files]
        lower_joined = "\n".join(lower_files)

        test_indicators = [
            "test_",
            "_test.",
            "/tests/",
            "tests/",
            ".spec.",
            ".test.",
            "pytest",
            "unittest",
            "jest",
            "mocha",
            "vitest",
        ]

        dependency_files = [
            "requirements.txt",
            "pyproject.toml",
            "package.json",
            "pom.xml",
            "build.gradle",
            "environment.yml",
            "dockerfile",
            "docker-compose.yml",
            "pipfile",
            "poetry.lock",
        ]

        has_tests = any(indicator in lower_joined for indicator in test_indicators)
        dependency_matches = [
            file for file in lower_files if os.path.basename(file) in dependency_files
        ]
        has_docs = bool(readme.strip()) or any("docs/" in file for file in lower_files)

        directories = set()

        for path in files:
            if "/" in path:
                directories.add(path.split("/", 1)[0])

        return {
            "file_count": len(files),
            "directory_count": len(directories),
            "has_tests": has_tests,
            "has_readme": bool(readme.strip()),
            "readme_length": len(readme or ""),
            "has_docs": has_docs,
            "dependency_files": dependency_matches[:10],
        }

    def _detect_frameworks_and_tools(
        self,
        repo_details: List[dict],
        language_breakdown: Dict[str, float],
    ) -> List[str]:
        text_parts: List[str] = []

        for repo in repo_details:
            text_parts.append(repo.get("name", ""))
            text_parts.append(repo.get("description", "") or "")
            text_parts.extend(repo.get("topics", []) or [])
            text_parts.append(repo.get("readme_preview", "") or "")
            text_parts.extend(repo.get("dependency_files", []) or [])

        text = " ".join(text_parts).lower()

        tool_keywords = {
            "PyTorch": ["pytorch", "torch"],
            "TensorFlow": ["tensorflow"],
            "HuggingFace": ["huggingface", "transformers"],
            "scikit-learn": ["scikit-learn", "sklearn"],
            "OpenCV": ["opencv", "cv2"],
            "Pandas": ["pandas"],
            "NumPy": ["numpy"],
            "FastAPI": ["fastapi"],
            "Flask": ["flask"],
            "Django": ["django"],
            "React": ["react"],
            "Node.js": ["node", "nodejs", "express"],
            "TypeScript": ["typescript"],
            "Docker": ["docker", "dockerfile"],
            "PostgreSQL": ["postgres", "postgresql"],
            "MySQL": ["mysql"],
            "DB2": ["db2"],
            "Jupyter": ["jupyter", "notebook"],
            "GitHub Actions": ["github actions", ".github/workflows"],
        }

        detected: List[str] = []

        for tool, keywords in tool_keywords.items():
            if any(keyword in text for keyword in keywords):
                detected.append(tool)

        for language in language_breakdown:
            if language not in detected:
                detected.append(language)

        return detected[:20]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyse(self, github_url_or_username: str) -> dict:
        """British spelling kept for compatibility with the existing main.py."""
        return self.analyze(github_url_or_username)

    def analyze(self, github_url_or_username: str) -> dict:
        """
        Main entry point. Accepts a GitHub profile URL or username.
        Returns a structured skills assessment.
        """
        try:
            username = self._extract_username(github_url_or_username)
        except Exception as exc:
            return {"error": str(exc)}

        console.print(f"[dim]Analysing GitHub profile: {username}[/dim]")

        profile = self._fetch_profile(username)

        if not profile:
            return {"error": f"GitHub user {username} not found, inaccessible, or rate-limited."}

        repos = self._fetch_repos(username)
        original_repos = [repo for repo in repos if not repo.get("fork", False)]
        forked_repos = [repo for repo in repos if repo.get("fork", False)]

        console.print(
            f"  Found {len(repos)} repos "
            f"({len(original_repos)} original, {len(forked_repos)} forked)"
        )

        all_languages: Dict[str, int] = {}
        repo_details: List[dict] = []
        all_commit_dates: List[str] = []
        total_commits = 0

        # Analyze top 12 recently updated original repos to balance detail vs API use.
        for repo in original_repos[:12]:
            repo_name = repo.get("name")

            if not repo_name:
                continue

            console.print(f"  [dim]Analysing repo: {repo_name}[/dim]")

            owner = repo.get("owner", {}).get("login", username)
            default_branch = repo.get("default_branch", "main")

            languages = self._fetch_languages(owner, repo_name)

            for language, byte_count in languages.items():
                all_languages[language] = all_languages.get(language, 0) + byte_count

            readme = self._fetch_readme(owner, repo_name)
            files = self._fetch_tree_files(owner, repo_name, default_branch)
            quality = self._repo_quality_signals(files, readme)

            commit_count, commit_dates = self._fetch_commit_count_and_dates(
                owner,
                repo_name,
                username,
            )

            total_commits += commit_count
            all_commit_dates.extend(commit_dates)

            repo_details.append(
                {
                    "name": repo_name,
                    "description": repo.get("description", "") or "",
                    "language": repo.get("language", "Unknown"),
                    "stars": repo.get("stargazers_count", 0),
                    "forks": repo.get("forks_count", 0),
                    "commits_by_student_approx": commit_count,
                    "has_readme": quality["has_readme"],
                    "readme_length": quality["readme_length"],
                    "readme_preview": readme[:500] if readme else "",
                    "has_tests": quality["has_tests"],
                    "has_docs": quality["has_docs"],
                    "dependency_files": quality["dependency_files"],
                    "file_count": quality["file_count"],
                    "directory_count": quality["directory_count"],
                    "topics": repo.get("topics", []) or [],
                    "updated_at": repo.get("updated_at", ""),
                    "created_at": repo.get("created_at", ""),
                    "size_kb": repo.get("size", 0),
                    "html_url": repo.get("html_url", ""),
                }
            )

        total_bytes = sum(all_languages.values()) or 1

        language_breakdown = {
            language: round((count / total_bytes) * 100, 1)
            for language, count in sorted(
                all_languages.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:10]
        }

        months_active = self._months_active(repos, all_commit_dates)
        frameworks_and_tools = self._detect_frameworks_and_tools(
            repo_details,
            language_breakdown,
        )

        raw_data = {
            "username": username,
            "name": profile.get("name") or username,
            "bio": profile.get("bio") or "",
            "public_repos": profile.get("public_repos", 0),
            "followers": profile.get("followers", 0),
            "following": profile.get("following", 0),
            "account_created": profile.get("created_at", ""),
            "months_active": months_active,
            "original_repos_count": len(original_repos),
            "forked_repos_count": len(forked_repos),
            "original_work_ratio": round(
                len(original_repos) / max(1, len(repos)),
                2,
            ),
            "total_commits_by_student_approx": total_commits,
            "language_breakdown_percent": language_breakdown,
            "frameworks_and_tools_detected": frameworks_and_tools,
            "repositories": repo_details,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        return self._assess(raw_data)

    # ------------------------------------------------------------------
    # Assessment
    # ------------------------------------------------------------------

    def _clean_model_json(self, raw: str) -> str:
        text = str(raw or "").strip()

        if text.startswith("```"):
            text = text.replace("```json", "")
            text = text.replace("```", "")
            text = text.strip()

        first_brace = text.find("{")
        last_brace = text.rfind("}")

        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            text = text[first_brace:last_brace + 1]

        return text.strip()

    def _assess(self, raw_data: dict) -> dict:
        """Use Claude to produce a structured skills assessment, with a rule fallback."""
        prompt = f"""
You are a technical skills evaluator for Kormic, a graduate admissions platform.
Evaluate this student's GitHub profile and produce an honest skills assessment.

Return ONLY valid JSON. No markdown. No explanation.

{{
  "primary_language": string,
  "languages": [{{"name": string, "percent": number, "level": "beginner|intermediate|advanced"}}],
  "frameworks_and_tools": [list of strings detected from repos],
  "months_active": integer,
  "work_consistency": "sporadic|moderate|consistent",
  "original_work_ratio": number,
  "strongest_repos": [{{"name": string, "why": string}}],
  "skill_signals": {{
    "ml_ai": boolean,
    "web_development": boolean,
    "data_engineering": boolean,
    "systems_programming": boolean,
    "mobile": boolean,
    "devops": boolean,
    "databases": boolean,
    "testing_discipline": boolean
  }},
  "overall_level": "beginner|developing|intermediate|strong|advanced",
  "strengths": [list of 2-4 specific evidence-based strengths],
  "honest_gaps": [list of 1-3 honest observations about what is missing or weak],
  "admissions_summary": string,
  "aria_notes": string
}}

Rules:
- Base every claim on the actual data provided.
- Never flatter. Honest gaps are as important as strengths.
- Treat forks/tutorials as weaker evidence than original repos.
- Testing, README quality, dependencies, structure, and commit consistency matter.
- Do not claim exact total commits are verified if marked approximate.
- For overall_level:
  beginner = mostly forks/tutorials or very shallow work;
  developing = some original work but shallow;
  intermediate = solid original projects with reasonable depth;
  strong = consistent work with good practices;
  advanced = exceptional depth and breadth.
- admissions_summary: 3-4 sentences a human admissions officer would find useful.
- aria_notes: one sentence flagging what Aria should remember when advising this student.

GITHUB DATA:
{json.dumps(raw_data, indent=2, ensure_ascii=False)}
"""

        try:
            client = _get_anthropic_client()

            response = client.messages.create(
                model=MODEL,
                max_tokens=1200,
                messages=[{"role": "user", "content": prompt}],
            )

            raw = response.content[0].text.strip()
            assessment = json.loads(self._clean_model_json(raw))

            if not isinstance(assessment, dict):
                raise ValueError("Claude assessment was not a JSON object.")

        except Exception as exc:
            console.print(f"[yellow]Claude GitHub assessment failed, using fallback: {exc}[/yellow]")
            assessment = self._fallback_assessment(raw_data)

        assessment["username"] = raw_data["username"]
       
        assessment["name"] = raw_data.get("name") or raw_data["username"]
        assessment["source"] = "github"
        assessment["verified"] = True
        assessment["generated_at"] = raw_data.get("generated_at")
        assessment["raw_signal_summary"] = {
            "public_repos": raw_data.get("public_repos"),
            "original_repos_count": raw_data.get("original_repos_count"),
            "forked_repos_count": raw_data.get("forked_repos_count"),
            "language_breakdown_percent": raw_data.get("language_breakdown_percent"),
            "total_commits_by_student_approx": raw_data.get("total_commits_by_student_approx"),
        }

        return assessment

    def _fallback_assessment(self, raw_data: dict) -> dict:
        """Rule-based fallback if the LLM call or JSON parsing fails."""
        language_breakdown = raw_data.get("language_breakdown_percent", {}) or {}
        primary_language = next(iter(language_breakdown.keys()), "Unknown")
        months_active = int(raw_data.get("months_active", 0) or 0)
        original_ratio = float(raw_data.get("original_work_ratio", 0) or 0)
        total_commits = int(raw_data.get("total_commits_by_student_approx", 0) or 0)
        repos = raw_data.get("repositories", []) or []
        tools = raw_data.get("frameworks_and_tools_detected", []) or []

        has_tests = any(repo.get("has_tests") for repo in repos)
        has_ml = any(
            tool in tools
            for tool in ["PyTorch", "TensorFlow", "HuggingFace", "scikit-learn", "OpenCV"]
        )
        has_web = any(
            tool in tools
            for tool in ["React", "Node.js", "FastAPI", "Flask", "Django"]
        )
        has_db = any(tool in tools for tool in ["PostgreSQL", "MySQL", "DB2"])
        has_devops = "Docker" in tools or "GitHub Actions" in tools

        if original_ratio < 0.35 or total_commits < 10:
            overall = "beginner"
        elif total_commits < 40 or months_active < 4:
            overall = "developing"
        elif total_commits < 150:
            overall = "intermediate"
        elif months_active >= 10:
            overall = "strong"
        else:
            overall = "intermediate"

        if total_commits >= 80 and months_active >= 6:
            consistency = "consistent"
        elif total_commits >= 20:
            consistency = "moderate"
        else:
            consistency = "sporadic"

        strengths = []

        if primary_language != "Unknown":
            strengths.append(f"Visible original work in {primary_language}.")

        if has_ml:
            strengths.append("Public projects show AI/ML tooling exposure.")

        if has_web:
            strengths.append("Public projects show web/backend development exposure.")

        if has_tests:
            strengths.append("Some repositories show testing discipline.")

        if not strengths:
            strengths.append("Some public coding activity is visible, but evidence is limited.")

        gaps = []

        if not has_tests:
            gaps.append("Testing evidence is limited or not visible in the analyzed repositories.")

        if original_ratio < 0.5:
            gaps.append("A significant share of public repositories appear to be forks or non-original work.")

        if total_commits < 30:
            gaps.append("Commit history is still thin, so skill depth should be interpreted cautiously.")

        if not gaps:
            gaps.append("More project documentation and collaboration evidence would strengthen the profile.")

        language_items = [
            {
                "name": name,
                "percent": percent,
                "level": (
                    "advanced"
                    if percent >= 60 and total_commits >= 100
                    else "intermediate"
                    if percent >= 25
                    else "beginner"
                ),
            }
            for name, percent in list(language_breakdown.items())[:5]
        ]

        return {
            "primary_language": primary_language,
            "languages": language_items,
            "frameworks_and_tools": tools[:12],
            "months_active": months_active,
            "work_consistency": consistency,
            "original_work_ratio": original_ratio,
            "strongest_repos": [
                {
                    "name": repo.get("name", "unknown"),
                    "why": "Recent original repository with visible structure.",
                }
                for repo in repos[:3]
            ],
            "skill_signals": {
                "ml_ai": has_ml,
                "web_development": has_web,
                "data_engineering": has_db,
                "systems_programming": False,
                "mobile": False,
                "devops": has_devops,
                "databases": has_db,
                "testing_discipline": has_tests,
            },
            "overall_level": overall,
            "strengths": strengths[:4],
            "honest_gaps": gaps[:3],
            "admissions_summary": (
                f"The public GitHub profile shows {overall} demonstrated technical evidence. "
                f"The strongest visible language is {primary_language}. "
                f"There are approximately {total_commits} visible authored commits across analyzed repositories, "
                f"with an original work ratio of {original_ratio}. This should be used as supporting evidence, not the only admissions signal."
            ),
            "aria_notes": (
                "Use the GitHub evidence to discuss demonstrated technical strength, "
                "but ask about private projects or internships before making a final judgment."
            ),
        }

    def print_summary(self, assessment: dict) -> None:
        if "error" in assessment:
            console.print(f"[red]GitHub analysis failed: {assessment['error']}[/red]")
            return

        strengths = (
            ", ".join(assessment.get("strengths", [])[:2])
            or "Not enough visible evidence"
        )

        console.print("\n[bold green]GitHub analysis complete.[/bold green]")
        console.print(f"  Username: @{assessment.get('username')}")
        console.print(f"  Primary language: {assessment.get('primary_language')}")
        console.print(
            f"  Overall level: {str(assessment.get('overall_level', 'unknown')).upper()}"
        )
        console.print(f"  Months active: {assessment.get('months_active')}")
        console.print(f"  Work consistency: {assessment.get('work_consistency')}")
        console.print(f"  Strengths: {strengths}")

        summary = assessment.get("admissions_summary", "")

        if summary:
            console.print(f"  [dim]{summary[:180]}...[/dim]")

        console.print()
