from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator


def _list_or_empty(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [item for item in value if item is not None]
    return [value]

class EvidenceValue(BaseModel):
    value: Optional[str] = None
    evidence: str = ""

class SkillObservation(BaseModel):
    name: Optional[str] = None
    evidence: str = ""

class ExperienceObservation(BaseModel):
    title: Optional[str] = None
    company: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    description: Optional[str] = None
    evidence: str = ""

class EducationObservation(BaseModel):
    institution: Optional[str] = None
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    evidence: str = ""

class ProjectObservation(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    technologies: list[str] = Field(default_factory=list)
    url: Optional[str] = None
    evidence: str = ""

    @field_validator("technologies", mode="before")
    @classmethod
    def normalize_technologies(cls, value):
        return _list_or_empty(value)

class PostObservation(BaseModel):
    text: Optional[str] = None
    date: Optional[str] = None
    topics: list[str] = Field(default_factory=list)
    evidence: str = ""

    @field_validator("topics", mode="before")
    @classmethod
    def normalize_topics(cls, value):
        return _list_or_empty(value)

class CertificationObservation(BaseModel):
    name: Optional[str] = None
    issuer: Optional[str] = None
    issue_date: Optional[str] = None
    credential_id: Optional[str] = None
    evidence: str = ""

class ImageObservation(BaseModel):
    name: Optional[EvidenceValue] = None
    headline: Optional[EvidenceValue] = None
    about: Optional[EvidenceValue] = None
    location: Optional[EvidenceValue] = None
    skills: list[SkillObservation] = Field(default_factory=list)
    experiences: list[ExperienceObservation] = Field(default_factory=list)
    education: list[EducationObservation] = Field(default_factory=list)
    projects: list[ProjectObservation] = Field(default_factory=list)
    posts: list[PostObservation] = Field(default_factory=list)
    certifications: list[CertificationObservation] = Field(default_factory=list)

    @field_validator(
        "skills",
        "experiences",
        "education",
        "projects",
        "posts",
        "certifications",
        mode="before",
    )
    @classmethod
    def normalize_lists(cls, value):
        return _list_or_empty(value)

    @classmethod
    def empty(cls):
        return cls()
