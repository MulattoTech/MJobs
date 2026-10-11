"""Cryptographically hashed, reproducible internal prompt/template versioning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import re
from typing import Mapping


class PromptTarget(StrEnum):
    RESUME = "resume"
    COVER_LETTER = "cover_letter"
    SCREENING_ANSWERS = "screening_answers"
    JOB_PARSING = "job_parsing"


PROMPT_TARGETS: tuple[str, ...] = tuple(t.value for t in PromptTarget)
_VERSION_REGEX = re.compile(r"^[0-9]+\.[0-9]+(\.[0-9]+)?(-[a-zA-Z0-9._-]+)?$")
_VAR_REGEX = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromptVersion:
    """An immutable, cryptographically hashed prompt template version."""

    target: str
    version: str
    template: str
    description: str
    variables: tuple[str, ...]
    sha256: str
    created_at_utc: str

    def __post_init__(self) -> None:
        if self.target not in PROMPT_TARGETS:
            raise ValueError(f"PROMPT_TARGET_INVALID: {self.target}")
        if not isinstance(self.version, str) or not _VERSION_REGEX.fullmatch(self.version):
            raise ValueError(f"PROMPT_VERSION_INVALID: {self.version}")
        if not isinstance(self.template, str) or not self.template.strip():
            raise ValueError("PROMPT_TEMPLATE_EMPTY")
        expected_hash = _sha256(self.template)
        if self.sha256 != expected_hash:
            raise ValueError("PROMPT_SHA256_MISMATCH")

    def render(self, **kwargs: object) -> str:
        """Render template with supplied keyword arguments, failing closed on missing keys."""
        missing = [var for var in self.variables if var not in kwargs]
        if missing:
            raise ValueError(f"PROMPT_VARIABLE_MISSING: {', '.join(missing)}")
        try:
            return self.template.format(**kwargs)
        except Exception as error:
            raise ValueError(f"PROMPT_RENDER_ERROR: {str(error)}") from None

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "version": self.version,
            "template": self.template,
            "description": self.description,
            "variables": list(self.variables),
            "sha256": self.sha256,
            "created_at_utc": self.created_at_utc,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "PromptVersion":
        return cls(
            target=str(data["target"]),
            version=str(data["version"]),
            template=str(data["template"]),
            description=str(data.get("description", "")),
            variables=tuple(data.get("variables", ())),
            sha256=str(data["sha256"]),
            created_at_utc=str(data.get("created_at_utc", "")),
        )


DEFAULT_PROMPT_TEMPLATES: dict[str, tuple[str, str]] = {
    PromptTarget.RESUME: (
        "Tailor resume facts for opportunity:\n"
        "Role: {job_title}\n"
        "Company: {company_name}\n"
        "Approved Career Facts:\n{career_facts}\n"
        "Instructions: Highlight matching experience without fabricating credentials.",
        "Canonical resume tailoring prompt template v1.0.0",
    ),
    PromptTarget.COVER_LETTER: (
        "Draft professional cover letter:\n"
        "Role: {job_title}\n"
        "Company: {company_name}\n"
        "Candidate Background:\n{candidate_profile}\n"
        "Key Qualifications:\n{qualifications}\n"
        "Instructions: Keep tone concise, professional, and grounded in provided profile.",
        "Canonical cover letter drafting prompt template v1.0.0",
    ),
    PromptTarget.SCREENING_ANSWERS: (
        "Synthesize screening question response:\n"
        "Question: {question_text}\n"
        "Role Context: {job_title} at {company_name}\n"
        "Approved Profile Answers:\n{approved_answers}\n"
        "Instructions: Answer accurately using only verified experience.",
        "Canonical screening question response prompt template v1.0.0",
    ),
    PromptTarget.JOB_PARSING: (
        "Extract structured job posting details:\n"
        "Raw Posting Content:\n{raw_content}\n"
        "Instructions: Extract title, company, location, salary range, and requirements.",
        "Canonical job parsing extraction prompt template v1.0.0",
    ),
}


def _extract_variables(template: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_VAR_REGEX.findall(template)))


class PromptRegistry:
    """Manages active and historical versions for all prompt targets."""

    def __init__(self) -> None:
        self._versions: dict[str, dict[str, PromptVersion]] = {target: {} for target in PROMPT_TARGETS}
        self._active: dict[str, str] = {}
        self._init_defaults()

    def _init_defaults(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        for target, (template, desc) in DEFAULT_PROMPT_TEMPLATES.items():
            version = "1.0.0"
            pv = PromptVersion(
                target=target,
                version=version,
                template=template,
                description=desc,
                variables=_extract_variables(template),
                sha256=_sha256(template),
                created_at_utc=now_iso,
            )
            self._versions[target][version] = pv
            self._active[target] = version

    def register_version(
        self,
        target: str | PromptTarget,
        version: str,
        template: str,
        description: str = "",
        set_active: bool = True,
    ) -> PromptVersion:
        t_str = str(target)
        if t_str not in PROMPT_TARGETS:
            raise ValueError(f"PROMPT_TARGET_INVALID: {t_str}")
        if not isinstance(version, str) or not _VERSION_REGEX.fullmatch(version):
            raise ValueError(f"PROMPT_VERSION_INVALID: {version}")
        if version in self._versions[t_str]:
            raise ValueError(f"PROMPT_VERSION_ALREADY_EXISTS: {version}")

        now_iso = datetime.now(timezone.utc).isoformat()
        pv = PromptVersion(
            target=t_str,
            version=version,
            template=template,
            description=description,
            variables=_extract_variables(template),
            sha256=_sha256(template),
            created_at_utc=now_iso,
        )
        self._versions[t_str][version] = pv
        if set_active:
            self._active[t_str] = version
        return pv

    def set_active_version(self, target: str | PromptTarget, version: str) -> None:
        t_str = str(target)
        if t_str not in PROMPT_TARGETS:
            raise ValueError(f"PROMPT_TARGET_INVALID: {t_str}")
        if version not in self._versions[t_str]:
            raise ValueError(f"PROMPT_VERSION_NOT_FOUND: {version}")
        self._active[t_str] = version

    def get_active_version(self, target: str | PromptTarget) -> PromptVersion:
        t_str = str(target)
        if t_str not in PROMPT_TARGETS:
            raise ValueError(f"PROMPT_TARGET_INVALID: {t_str}")
        active_ver = self._active.get(t_str)
        if not active_ver or active_ver not in self._versions[t_str]:
            raise ValueError(f"PROMPT_ACTIVE_VERSION_MISSING: {t_str}")
        return self._versions[t_str][active_ver]

    def get_version(self, target: str | PromptTarget, version: str) -> PromptVersion:
        t_str = str(target)
        if t_str not in PROMPT_TARGETS:
            raise ValueError(f"PROMPT_TARGET_INVALID: {t_str}")
        pv = self._versions[t_str].get(version)
        if pv is None:
            raise ValueError(f"PROMPT_VERSION_NOT_FOUND: {version}")
        return pv

    def list_versions(self, target: str | PromptTarget) -> list[PromptVersion]:
        t_str = str(target)
        if t_str not in PROMPT_TARGETS:
            raise ValueError(f"PROMPT_TARGET_INVALID: {t_str}")
        return list(self._versions[t_str].values())

    def render(
        self,
        target: str | PromptTarget,
        version: str | None = None,
        **kwargs: object,
    ) -> tuple[str, str]:
        """Render a versioned prompt; returns (rendered_prompt, version_identifier)."""
        t_str = str(target)
        pv = self.get_version(t_str, version) if version else self.get_active_version(t_str)
        rendered = pv.render(**kwargs)
        return rendered, pv.version

    def to_dict(self) -> dict[str, object]:
        return {
            "active": dict(self._active),
            "versions": {
                target: [pv.to_dict() for pv in versions.values()]
                for target, versions in self._versions.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "PromptRegistry":
        reg = cls()
        reg._versions = {target: {} for target in PROMPT_TARGETS}
        raw_versions = data.get("versions", {})
        if isinstance(raw_versions, Mapping):
            for target, v_list in raw_versions.items():
                if target in PROMPT_TARGETS and isinstance(v_list, list):
                    for item in v_list:
                        pv = PromptVersion.from_dict(item)
                        reg._versions[target][pv.version] = pv
        raw_active = data.get("active", {})
        if isinstance(raw_active, Mapping):
            for target, ver in raw_active.items():
                if target in reg._versions and str(ver) in reg._versions[target]:
                    reg._active[target] = str(ver)
        return reg


__all__ = [
    "DEFAULT_PROMPT_TEMPLATES",
    "PROMPT_TARGETS",
    "PromptRegistry",
    "PromptTarget",
    "PromptVersion",
]
