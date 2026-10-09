"""Versioned, factual-content-free resume template definitions and selection."""

from dataclasses import dataclass

from services.documents.errors import PreparationError


@dataclass(frozen=True)
class ResumeTemplate:
    template_id: str
    version: str
    display_name: str
    description: str
    section_order: tuple[str, ...]

    def as_public_dict(self) -> dict:
        """Return a fresh JSON-safe definition suitable for a preview client."""
        return {
            'template_id': self.template_id,
            'version': self.version,
            'display_name': self.display_name,
            'description': self.description,
            'ats_safe': True,
            'layout': {
                'column_count': 1,
                'graphics': False,
                'tables': False,
                'text_only': True,
                'section_order': list(self.section_order),
            },
        }


# Definitions describe structure only. They intentionally contain no profile data,
# fact text, keyword claims, or generated content.
_TEMPLATES = (
    ResumeTemplate(
        template_id='verified-headings-v1',
        version='1',
        display_name='Classic',
        description='A conventional single-column layout with experience before skills.',
        section_order=(
            'summary', 'experience', 'skill', 'education', 'certification', 'project',
        ),
    ),
    ResumeTemplate(
        template_id='approved-skill-order-headings-v1',
        version='1',
        display_name='Skills Forward',
        description='A single-column layout that places approved skills before experience.',
        section_order=(
            'summary', 'skill', 'experience', 'project', 'education', 'certification',
        ),
    ),
    ResumeTemplate(
        template_id='approved-fact-selection-headings-v1',
        version='1',
        display_name='Experience First',
        description='A single-column chronological layout that places experience first.',
        section_order=(
            'experience', 'summary', 'skill', 'education', 'certification', 'project',
        ),
    ),
)
_TEMPLATES_BY_ID = {template.template_id: template for template in _TEMPLATES}


def list_resume_templates() -> list[dict]:
    """List supported, versioned ATS-safe layout definitions in stable order."""
    return [template.as_public_dict() for template in _TEMPLATES]


def select_resume_template(template_id, expected_version) -> dict:
    """Validate an explicit, version-pinned template choice for preview.

    There is intentionally no implicit default here: a caller must provide both
    the selected catalog ID and the version it displayed to the user.
    """
    if (type(template_id) is not str or not template_id or template_id.strip() != template_id or
            len(template_id) > 100):
        raise PreparationError(
            'RESUME_TEMPLATE_SELECTION_INVALID',
            'Select a supported resume template.',
        )
    template = _TEMPLATES_BY_ID.get(template_id)
    if template is None:
        raise PreparationError(
            'RESUME_TEMPLATE_SELECTION_INVALID',
            'Select a supported resume template.',
        )
    if expected_version is None:
        raise PreparationError(
            'RESUME_TEMPLATE_VERSION_REQUIRED',
            'Reload the template catalog and select the displayed version.',
        )
    if type(expected_version) is not str or expected_version != template.version:
        raise PreparationError(
            'RESUME_TEMPLATE_VERSION_STALE',
            'The selected resume template changed; reload the catalog before previewing.',
        )

    selection = template.as_public_dict()
    selection['selection_mode'] = 'EXPLICIT'
    selection['preview_only'] = True
    return selection
