"""Explicit immutable resume contracts; headings never invent career claims."""
from services.documents.errors import PreparationError

BASE_TEMPLATE = 'verified-headings-v1'
BASE_MODEL = 'deterministic-fact-copy-v2'
TAILORED_TEMPLATE = 'approved-skill-order-headings-v1'
TAILORED_MODEL = 'deterministic-skill-order-v2'
SELECTION_TEMPLATE = 'approved-fact-selection-headings-v1'
SELECTION_MODEL = 'deterministic-fact-selection-v1'
CONTRACTS = {
    'verified-facts-v1': ('deterministic-fact-copy-v1', 'BASE', False),
    'approved-skill-order-v1': ('deterministic-skill-order-v1', 'TAILORED', False),
    BASE_TEMPLATE: (BASE_MODEL, 'BASE', True),
    TAILORED_TEMPLATE: (TAILORED_MODEL, 'TAILORED', True),
    SELECTION_TEMPLATE: (SELECTION_MODEL, 'TAILORED', True),
}
HEADINGS = {'summary':'Summary', 'experience':'Experience', 'responsibility':'Experience',
    'skill':'Skills', 'technology_experience':'Technology Experience', 'education':'Education',
    'certification':'Certifications', 'project':'Projects', 'accomplishment':'Accomplishments',
    'career_goal':'Career Goals'}


def contract_role(template, model, allow_legacy_unrecorded=False):
    contract = CONTRACTS.get(template)
    if not contract:
        return None
    if model == contract[0] or (allow_legacy_unrecorded and model is None and not contract[2]):
        return contract[1]
    return None


def resume_layout(profile, facts, template):
    contract = CONTRACTS.get(template)
    if not contract:
        raise PreparationError('UNSUPPORTED_CLAIM', 'Document template has no assertion validation contract')
    paragraphs = [profile['name'], profile['email']]
    headings = set()
    previous = None
    for row in facts:
        if contract[2]:
            heading = HEADINGS.get(row['kind'], 'Additional Information')
            if heading != previous:
                headings.add(len(paragraphs))
                paragraphs.append(heading)
                previous = heading
        paragraphs.append(row['text'])
    return paragraphs, headings
