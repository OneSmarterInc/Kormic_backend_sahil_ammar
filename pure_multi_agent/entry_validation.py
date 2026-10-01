"""Completeness contracts for proposed university records, not chat answers."""
import re
from datetime import date
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


def reject_placeholders(values):
    for key, value in values.items():
        if isinstance(value, str) and value.strip().casefold() in ('', 'tbd', 'unknown', 'not specified', 'to be confirmed', 'to be decided', 'n/a', '?'):
            raise ValueError(f'{key} is incomplete; ask for its actual value.')
        if isinstance(value, dict):
            reject_placeholders(value)
        elif isinstance(value, list):
            reject_placeholders({f'{key}[{index}]': item for index, item in enumerate(value)})


class CompleteRequirement(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, allow_inf_nan=False)
    criterion: str = Field(min_length=2, max_length=500)
    detail: str = Field(min_length=10, max_length=2000)
    category: Literal['gpa', 'test_score', 'experience', 'degree', 'document', 'deadline', 'other']
    applies_to: str = Field(min_length=3, max_length=500, description='Program/level/applicant scope, explicitly stated or confirmed as all applicants.')
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    scale_maximum: Optional[float] = None
    test_name: Optional[str] = None
    unit: Optional[Literal['months', 'years']] = None
    accepted_qualifications: Optional[list[str]] = None
    required_documents: Optional[list[str]] = None
    deadline: Optional[str] = Field(default=None, description='Exact ISO date YYYY-MM-DD including year.')
    intake: Optional[str] = None

    @model_validator(mode='after')
    def complete(self):
        reject_placeholders(self.model_dump())
        title = self.criterion.casefold()
        if re.search(r'\b(cgpa|gpa|grade point)\b', title) and self.category != 'gpa':
            raise ValueError('CGPA/GPA requires category gpa, minimum, maximum, grading scale and applicant scope.')
        if re.search(r'\b(ielts|toefl|gre|gmat|sat|duolingo|pte)\b', title) and self.category != 'test_score':
            raise ValueError('A test requirement needs its test name, score range, scale and applicant scope.')
        if self.category in ('gpa', 'test_score'):
            if self.minimum is None or self.maximum is None or self.scale_maximum is None:
                raise ValueError('Ask for the minimum, maximum and grading/test scale before proposing this requirement. Do not assume a 4.0 or 10.0 scale.')
            if not 0 <= self.minimum <= self.maximum <= self.scale_maximum or self.scale_maximum <= 0:
                raise ValueError('Score range must satisfy 0 <= minimum <= maximum <= grading scale.')
            if self.category == 'test_score' and not self.test_name:
                raise ValueError('Specify the test name and version (for example IELTS Academic or TOEFL iBT).')
        elif self.category == 'experience':
            if self.minimum is None or self.minimum < 0 or not self.unit:
                raise ValueError('Experience requires a minimum duration and months/years unit.')
            if self.maximum is not None and self.maximum < self.minimum:
                raise ValueError('Maximum experience cannot be below the minimum.')
        elif self.category == 'degree' and not self.accepted_qualifications:
            raise ValueError('Specify the accepted degree/qualification(s).')
        elif self.category == 'document' and not self.required_documents:
            raise ValueError('Specify the required documents.')
        elif self.category == 'deadline':
            if not self.deadline or not self.intake:
                raise ValueError('A deadline needs its exact date/year, intake and applicant scope.')
            date.fromisoformat(self.deadline)
        return self


class PolicyDetails(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, allow_inf_nan=False)
    category: Literal['scholarship', 'tuition', 'course', 'intake', 'deadline', 'requirement', 'general']
    applies_to: str = Field(min_length=3, max_length=1000)
    effective_period: str = Field(min_length=3, max_length=300, description='Applicable intake/year or explicitly confirmed ongoing policy.')
    eligibility: Optional[str] = None
    amount: Optional[float] = Field(default=None, ge=0)
    currency: Optional[str] = Field(default=None, pattern=r'^[A-Z]{3}$')
    amount_basis: Optional[str] = Field(default=None, description='e.g. per year, per semester, total award, full tuition waiver.')
    coverage: Optional[str] = None
    application_process: Optional[str] = None
    deadline: Optional[str] = Field(default=None, description='YYYY-MM-DD, or explicitly confirmed rolling/no deadline.')
    course_name: Optional[str] = None
    level: Optional[str] = None
    duration: Optional[str] = None
    study_mode: Optional[str] = None
    requirements: Optional[str] = None
    term: Optional[str] = None
    year: Optional[int] = Field(default=None, ge=1900, le=2200)
    requirement: Optional[CompleteRequirement] = None

    @model_validator(mode='after')
    def complete(self):
        reject_placeholders(self.model_dump())
        required = {
            'scholarship': ('eligibility', 'application_process', 'deadline'),
            'tuition': ('amount', 'currency', 'amount_basis'),
            'course': ('course_name', 'level', 'duration', 'study_mode', 'requirements'),
            'intake': ('term', 'year', 'deadline', 'application_process'),
            'deadline': ('deadline', 'term', 'year'),
            'requirement': ('requirement',),
            'general': (),
        }[self.category]
        missing = [key for key in required if getattr(self, key) is None or getattr(self, key) == '']
        if self.category == 'scholarship':
            if self.amount is not None:
                missing += [key for key in ('currency', 'amount_basis') if not getattr(self, key)]
            elif not self.coverage:
                missing.append('amount with currency/basis OR explicit award coverage')
        if missing:
            raise ValueError('Incomplete entry. Ask the officer for: ' + ', '.join(missing) + '. Never invent these values.')
        if self.deadline and self.deadline.casefold() not in ('rolling', 'no deadline'):
            date.fromisoformat(self.deadline)
        return self


def validate_policy(topic, details):
    parsed = PolicyDetails.model_validate(details)
    title = topic.casefold()
    patterns = {'scholarship': r'\b(scholarship|bursary|fellowship|financial aid)\b',
        'tuition': r'\b(tuition|fees?)\b', 'intake': r'\bintakes?\b'}
    for category, pattern in patterns.items():
        if re.search(pattern, title) and parsed.category not in (category, 'deadline'):
            raise ValueError(f'This topic requires complete {category} details, not general notes.')
    if re.search(r'\b(cgpa|gpa|ielts|toefl|gre|gmat)\b', title) and parsed.category != 'requirement':
        raise ValueError('Numeric admission requirements need a complete structured requirement including score range and scale.')
    return parsed.model_dump(exclude_none=True)
