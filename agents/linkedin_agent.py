# agents/linkedin_agent.py
# LinkedIn Agent for Kormic Commons.
# Extracts structured student profile signals from LinkedIn screenshots
# or pasted/exported LinkedIn text. It does not scrape or log in to LinkedIn.

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any, Dict, List

import anthropic
from rich.console import Console

console = Console()

# Bounds the extraction call so a hung upstream request can't hold the
# request worker indefinitely (see LinkedInAnalyzeAPIView).
ANTHROPIC_CLIENT_TIMEOUT_SECONDS = 120.0
client = anthropic.Anthropic(timeout=ANTHROPIC_CLIENT_TIMEOUT_SECONDS, max_retries=1)

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1200

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_TEXT_EXTENSIONS = {".txt", ".md"}

LINKEDIN_EXTRACTION_PROMPT = """
You are the LinkedIn Agent for Kormic Commons.

Your job is to extract a student's professional and educational profile from
LinkedIn screenshots or pasted LinkedIn text.

Return ONLY valid JSON. Do not use markdown. Do not explain.

Required JSON format:
{
  "source": "linkedin_screenshot_or_text",
  "verified": false,
  "confidence_level": "low|medium|high",
  "name": string or null,
  "headline": string or null,
  "location": string or null,
  "linkedin_url": string or null,
  "current_role": string or null,
  "current_company": string or null,
  "education": [
    {
      "institution": string or null,
      "degree": string or null,
      "field_of_study": string or null,
      "start_year": integer or null,
      "end_year": integer or null,
      "details": string or null
    }
  ],
  "experience": [
    {
      "title": string or null,
      "company": string or null,
      "duration": string or null,
      "location": string or null,
      "summary": string or null
    }
  ],
  "skills": [list of strings],
  "certifications": [list of strings],
  "projects": [
    {
      "title": string or null,
      "description": string or null,
      "technologies": [list of strings]
    }
  ],
  "volunteering": [list of strings],
  "languages": [list of strings],
  "professional_interests": [list of strings],
  "career_direction": string or null,
  "missing_sections": [list of important sections not visible],
  "confidence_notes": string
}

Rules:
- Never invent information.
- If the screenshot is unclear or a section is not visible, use null or an empty list.
- LinkedIn screenshots are not official verification, so verified must be false.
- Use confidence_level high only if the profile top, education, and experience are clearly visible.
- If only one screenshot is provided and many sections are missing, confidence_level should usually be low or medium.
- Keep summaries short and factual.
"""


class LinkedInAgent:
    """Extract LinkedIn profile facts from screenshots or text files."""

    def _image_block(self, path: Path) -> Dict[str, Any]:
        media_type = mimetypes.guess_type(str(path))[0] or "image/png"
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        }

    def _text_block(self, path: Path) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        return {
            "type": "text",
            "text": f"LINKEDIN TEXT FILE: {path.name}\n\n{text[:12000]}",
        }

    def _build_content(self, paths: List[str]) -> List[Dict[str, Any]]:
        content: List[Dict[str, Any]] = []

        for raw_path in paths:
            path = Path(raw_path)
            if not path.exists():
                raise FileNotFoundError(f"LinkedIn file not found: {raw_path}")

            suffix = path.suffix.lower()

            if suffix in SUPPORTED_IMAGE_EXTENSIONS:
                content.append(self._image_block(path))
            elif suffix in SUPPORTED_TEXT_EXTENSIONS:
                content.append(self._text_block(path))
            else:
                raise ValueError(
                    f"Unsupported LinkedIn input format: {suffix}. "
                    "Use PNG/JPG/JPEG/WEBP screenshots or TXT/MD text export."
                )

        content.append({"type": "text", "text": LINKEDIN_EXTRACTION_PROMPT})
        return content

    def extract(self, paths: List[str]) -> Dict[str, Any]:
        """Run one Claude call for the whole LinkedIn screenshot/text batch."""
        if not paths:
            raise ValueError("No LinkedIn screenshots or text files were provided.")

        console.print(
            f"[yellow]LinkedIn Agent extracting profile from {len(paths)} file(s)...[/yellow]"
        )

        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": self._build_content(paths)}],
            )
        except Exception as exc:
            raise RuntimeError(
                "LinkedIn extraction failed because Claude was unavailable. "
                f"Reason: {exc}"
            ) from exc

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:-1]).strip()

        try:
            extracted = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "LinkedIn extraction returned invalid JSON. "
                f"Raw response: {raw[:500]}"
            ) from exc

        extracted["source"] = extracted.get("source") or "linkedin_screenshot_or_text"
        extracted["verified"] = False
        extracted["input_files"] = [Path(p).name for p in paths]
        return extracted

    def print_summary(self, linkedin_profile: Dict[str, Any]):
        console.print("\n[bold green]LinkedIn extraction complete.[/bold green]")
        console.print(f"  Name: {linkedin_profile.get('name')}")
        console.print(f"  Headline: {linkedin_profile.get('headline')}")
        console.print(f"  Current role: {linkedin_profile.get('current_role')}")

        skills = linkedin_profile.get("skills", []) or []
        if skills:
            console.print(f"  Skills: {', '.join(skills[:8])}")

        missing = linkedin_profile.get("missing_sections", []) or []
        if missing:
            console.print(f"  [yellow]Missing/unclear: {', '.join(missing[:6])}[/yellow]")

        notes = linkedin_profile.get("confidence_notes")
        if notes:
            console.print(f"  [dim]{notes}[/dim]")
        console.print()
