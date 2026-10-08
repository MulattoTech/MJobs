"""Explicit optional profile values; preferences never imply verified answers."""
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ShortText = Annotated[str, Field(max_length=300)]
AvoidTerm = Annotated[str, Field(min_length=1, max_length=120)]
SensitiveAnswerCategory = Literal[
    'RACE_ETHNICITY',
    'RELIGION',
    'SEX_GENDER',
    'DISABILITY',
    'VETERAN_STATUS',
    'OTHER_VOLUNTARY_DEMOGRAPHIC',
]
SensitiveAnswerMode = Literal[
    'PREFER_NOT_TO_ANSWER',
    'USE_CONFIGURED_RESPONSE',
    'ALWAYS_REQUIRE_REVIEW',
]


class ContactDetails(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    first_name: ShortText = ''
    middle_name: ShortText = ''
    last_name: ShortText = ''
    preferred_name: ShortText = ''
    phones: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(default_factory=list, max_length=10)
    address_line1: ShortText = ''
    address_line2: ShortText = ''
    city: ShortText = ''
    state: ShortText = ''
    postal_code: ShortText = ''
    country: ShortText = ''
    linkedin: str = Field(default='', max_length=1000)
    github: str = Field(default='', max_length=1000)
    portfolio: str = Field(default='', max_length=1000)

    @field_validator('linkedin', 'github', 'portfolio')
    @classmethod
    def safe_link(cls, value):
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in value):
            raise ValueError('Use an http or https profile link without credentials')
        return value


class JobPreferences(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    desired_roles: list[ShortText] = Field(default_factory=list, max_length=30)
    employment_types: list[Literal['FULL_TIME', 'PART_TIME', 'CONTRACT', 'CONSULTING', 'PER_DIEM', 'TEMPORARY', 'INTERNSHIP']] = Field(default_factory=list, max_length=7)
    work_arrangements: list[Literal['REMOTE', 'HYBRID', 'ON_SITE']] = Field(default_factory=list, max_length=3)
    shifts: list[Literal['DAY', 'EVENING', 'NIGHT', 'FLEXIBLE']] = Field(default_factory=list, max_length=4)
    max_commute_miles: float | None = Field(default=None, ge=0, le=1000)
    max_commute_minutes: int | None = Field(default=None, ge=0, le=1440)
    travel_preference: ShortText = ''
    salary_expectations: ShortText = ''
    work_authorization: ShortText = ''
    sponsorship_needs: Literal['NOT_SPECIFIED', 'REQUIRED', 'NOT_REQUIRED'] = 'NOT_SPECIFIED'
    career_goals: str = Field(default='', max_length=3000)
    avoid_terms: list[AvoidTerm] = Field(default_factory=list, max_length=50)
    sensitive_answer_policies: dict[SensitiveAnswerCategory, 'SensitiveAnswerPolicy'] = Field(
        default_factory=dict, max_length=6,
    )

    def sensitive_answer_policy_for(self, category: SensitiveAnswerCategory) -> 'SensitiveAnswerPolicy':
        """Return the stored policy or the safe review-required default."""
        return self.sensitive_answer_policies.get(category, SensitiveAnswerPolicy())

    @field_validator('avoid_terms')
    @classmethod
    def normalize_avoid_terms(cls, values):
        normalized = []
        seen = set()
        for value in values:
            term = value.strip()
            if not term:
                raise ValueError('Avoid terms cannot be blank')
            key = term.casefold()
            if key not in seen:
                normalized.append(term)
                seen.add(key)
        return normalized


class SensitiveAnswerPolicy(BaseModel):
    """Explicit user preference for one sensitive application-answer category."""

    model_config = ConfigDict(strict=True, extra='forbid')
    mode: SensitiveAnswerMode = 'ALWAYS_REQUIRE_REVIEW'
    response: str | None = Field(default=None, max_length=500)

    @field_validator('response')
    @classmethod
    def normalize_response(cls, value):
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError('Configured response cannot be blank')
        return value

    @model_validator(mode='after')
    def validate_response_mode(self):
        if self.mode == 'USE_CONFIGURED_RESPONSE' and self.response is None:
            raise ValueError('USE_CONFIGURED_RESPONSE requires an explicit response')
        if self.mode != 'USE_CONFIGURED_RESPONSE' and self.response is not None:
            raise ValueError('A response is allowed only for USE_CONFIGURED_RESPONSE')
        return self
