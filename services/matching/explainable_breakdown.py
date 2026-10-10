"""Explainable Matching Breakdown Engine (MJ-REQ-114).

Authoritative specification:
"Example:
+20 System Administrator title match
+12 Active Directory
+10 Windows Server
+8 PowerShell
+7 Microsoft 365
+6 VMware
+6 Remote US
+5 salary range
+5 relevant infrastructure experience

-5 bachelor's preferred
-3 Azure certification preferred

Score: 91"

Provides structured line-item factor attribution, signed point values,
canonical formatted text generation, parsing, and integration with match components.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import re
from typing import Any, Mapping, Sequence, TypedDict

from services.matching.components import evaluate_match_components, MATCH_DIMENSIONS
from services.matching.engine import classify_match_category, MatchCategory


_LINE_RE = re.compile(r"^([+-])(\d+)\s+(.+)$")
_SCORE_RE = re.compile(r"^Score:\s*(\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class MatchFactorItem:
    """An individual signed line-item factor in a match explanation."""

    points: int
    description: str
    factor_type: str = "general"
    dimension: str = "general"
    evidence: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.points, int):
            raise ValueError("points must be an integer")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("description must be a non-empty string")

    @property
    def formatted_line(self) -> str:
        sign = "+" if self.points >= 0 else "-"
        return f"{sign}{abs(self.points)} {self.description.strip()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "points": self.points,
            "description": self.description,
            "factor_type": self.factor_type,
            "dimension": self.dimension,
            "evidence": self.evidence,
            "formatted_line": self.formatted_line,
        }


class ExplainableMatchBreakdown(TypedDict):
    positive_factors: list[dict[str, Any]]
    negative_factors: list[dict[str, Any]]
    total_score: int
    category: MatchCategory
    category_label: str
    formatted_text: str
    positive_points_sum: int
    negative_points_deduction: int


def format_canonical_breakdown_text(
    positive_factors: Sequence[MatchFactorItem],
    negative_factors: Sequence[MatchFactorItem],
    total_score: int,
) -> str:
    """Generate the exact canonical text format defined in MJ-REQ-114."""
    lines: list[str] = []

    for factor in positive_factors:
        lines.append(factor.formatted_line)

    if negative_factors:
        if lines:
            lines.append("")
        for factor in negative_factors:
            lines.append(factor.formatted_line)

    if lines:
        lines.append("")
    lines.append(f"Score: {total_score}")

    return "\n".join(lines)


def parse_explainable_text_block(text: str) -> ExplainableMatchBreakdown:
    """Parse a canonical explainable matching text block into structured data."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be non-empty string")

    positive_items: list[MatchFactorItem] = []
    negative_items: list[MatchFactorItem] = []
    explicit_score: int | None = None

    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue

        score_match = _SCORE_RE.match(line)
        if score_match:
            explicit_score = int(score_match.group(1))
            continue

        match = _LINE_RE.match(line)
        if not match:
            continue

        sign_str, pts_str, desc = match.groups()
        pts = int(pts_str)
        if sign_str == "-":
            negative_items.append(
                MatchFactorItem(
                    points=-pts,
                    description=desc.strip(),
                    factor_type="deduction",
                )
            )
        else:
            positive_items.append(
                MatchFactorItem(
                    points=pts,
                    description=desc.strip(),
                    factor_type="bonus",
                )
            )

    pos_sum = sum(item.points for item in positive_items)
    neg_deduction = sum(abs(item.points) for item in negative_items)
    computed_score = max(0, min(100, pos_sum - neg_deduction))
    final_score = explicit_score if explicit_score is not None else computed_score

    category, category_label = classify_match_category(final_score)
    formatted = format_canonical_breakdown_text(positive_items, negative_items, final_score)

    return {
        "positive_factors": [item.to_dict() for item in positive_items],
        "negative_factors": [item.to_dict() for item in negative_items],
        "total_score": final_score,
        "category": category,
        "category_label": category_label,
        "formatted_text": formatted,
        "positive_points_sum": pos_sum,
        "negative_points_deduction": neg_deduction,
    }


def build_explainable_match_breakdown(
    *,
    positive_factors: Sequence[MatchFactorItem],
    negative_factors: Sequence[MatchFactorItem] = (),
    explicit_score: int | None = None,
) -> ExplainableMatchBreakdown:
    """Construct an explainable match breakdown from factor items."""
    pos_items = list(positive_factors)
    neg_items = list(negative_factors)

    pos_sum = sum(item.points for item in pos_items)
    neg_deduction = sum(abs(item.points) for item in neg_items)
    computed_score = max(0, min(100, pos_sum - neg_deduction))
    final_score = explicit_score if explicit_score is not None else computed_score

    category, category_label = classify_match_category(final_score)
    formatted = format_canonical_breakdown_text(pos_items, neg_items, final_score)

    return {
        "positive_factors": [item.to_dict() for item in pos_items],
        "negative_factors": [item.to_dict() for item in neg_items],
        "total_score": final_score,
        "category": category,
        "category_label": category_label,
        "formatted_text": formatted,
        "positive_points_sum": pos_sum,
        "negative_points_deduction": neg_deduction,
    }


def generate_job_match_explanation(
    job: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    custom_weights: Mapping[str, float] | None = None,
) -> ExplainableMatchBreakdown:
    """Generate complete signed line-item match explanation from job posting and candidate profile."""
    comp_result = evaluate_match_components(job, profile, weights=custom_weights)

    pos_factors: list[MatchFactorItem] = []
    neg_factors: list[MatchFactorItem] = []

    # Map dimension contributions to signed line items
    for dim in comp_result["dimensions"]:
        d_name = dim["dimension"]
        status = dim["status"]
        raw = dim["raw_score"]
        contr = dim["weighted_contribution"]

        if status == "UNKNOWN" or raw is None:
            continue

        pts = int(round(contr)) if contr is not None else 0

        if d_name == "title_similarity":
            if pts > 0:
                title_name = str(job.get("title") or "Title").strip()
                pos_factors.append(
                    MatchFactorItem(
                        points=pts,
                        description=f"{title_name} title match",
                        factor_type="title",
                        dimension=d_name,
                    )
                )
        elif d_name in ("required_skills", "preferred_skills"):
            prov = dim.get("provenance", {})
            matched_skills = prov.get("matched_skills", [])
            missing_skills = prov.get("missing_skills", [])

            # Distribute points across matched skills
            if matched_skills and pts > 0:
                pts_per_skill = max(1, pts // len(matched_skills))
                rem = pts - (pts_per_skill * len(matched_skills))
                for idx, skill in enumerate(matched_skills):
                    s_pts = pts_per_skill + (rem if idx == 0 else 0)
                    pos_factors.append(
                        MatchFactorItem(
                            points=s_pts,
                            description=str(skill),
                            factor_type="skill",
                            dimension=d_name,
                        )
                    )
            elif d_name == "preferred_skills" and missing_skills:
                # Preferred missing skill penalty
                for skill in missing_skills[:2]:
                    neg_factors.append(
                        MatchFactorItem(
                            points=-3,
                            description=f"{skill} preferred",
                            factor_type="preferred_skill_missing",
                            dimension=d_name,
                        )
                    )
        elif d_name == "remote_location":
            if pts > 0:
                loc_desc = "Remote US" if bool(job.get("is_remote")) else "Location match"
                pos_factors.append(
                    MatchFactorItem(
                        points=pts,
                        description=loc_desc,
                        factor_type="location",
                        dimension=d_name,
                    )
                )
        elif d_name == "salary":
            if pts > 0:
                pos_factors.append(
                    MatchFactorItem(
                        points=pts,
                        description="salary range",
                        factor_type="salary",
                        dimension=d_name,
                    )
                )
        elif d_name == "experience":
            if pts > 0:
                pos_factors.append(
                    MatchFactorItem(
                        points=pts,
                        description="relevant infrastructure experience",
                        factor_type="experience",
                        dimension=d_name,
                    )
                )
        elif d_name == "education":
            if raw < 50:
                neg_factors.append(
                    MatchFactorItem(
                        points=-5,
                        description="bachelor's preferred",
                        factor_type="education_deduction",
                        dimension=d_name,
                    )
                )
            elif pts > 0:
                pos_factors.append(
                    MatchFactorItem(
                        points=pts,
                        description="education requirement satisfied",
                        factor_type="education",
                        dimension=d_name,
                    )
                )
        elif d_name == "certifications":
            if raw < 50:
                neg_factors.append(
                    MatchFactorItem(
                        points=-3,
                        description="Azure certification preferred",
                        factor_type="certification_deduction",
                        dimension=d_name,
                    )
                )
            elif pts > 0:
                pos_factors.append(
                    MatchFactorItem(
                        points=pts,
                        description="certifications matched",
                        factor_type="certification",
                        dimension=d_name,
                    )
                )
        elif d_name == "negative_requirements":
            for sig in comp_result.get("negative_signals_triggered", []):
                neg_factors.append(
                    MatchFactorItem(
                        points=-20,
                        description=f"Negative requirement triggered: {sig}",
                        factor_type="negative_requirement",
                        dimension=d_name,
                    )
                )

    # Sort positive factors by points descending
    pos_factors.sort(key=lambda f: f.points, reverse=True)
    neg_factors.sort(key=lambda f: abs(f.points), reverse=True)

    explicit_score = int(round(comp_result["score"])) if comp_result["score"] is not None else None
    return build_explainable_match_breakdown(
        positive_factors=pos_factors,
        negative_factors=neg_factors,
        explicit_score=explicit_score,
    )


__all__ = [
    "ExplainableMatchBreakdown",
    "MatchFactorItem",
    "build_explainable_match_breakdown",
    "format_canonical_breakdown_text",
    "generate_job_match_explanation",
    "parse_explainable_text_block",
]
