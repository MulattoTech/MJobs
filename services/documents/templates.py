"""Explicit immutable resume contracts; headings never invent career claims."""
from services.documents.errors import PreparationError

BASE_TEMPLATE = 'verified-headings-v1'
BASE_MODEL = 'deterministic-fact-copy-v2'
TAILORED_TEMPLATE = 'approved-skill-emphasis-v1'
TAILORED_MODEL = 'deterministic-skill-order-v3'
SELECTION_TEMPLATE = 'approved-selection-emphasis-v1'
SELECTION_MODEL = 'deterministic-fact-selection-v2'
LEGACY_TAILORED_TEMPLATE = TAILORED_TEMPLATE
LEGACY_TAILORED_MODEL = TAILORED_MODEL
LEGACY_SELECTION_TEMPLATE = SELECTION_TEMPLATE
LEGACY_SELECTION_MODEL = SELECTION_MODEL
TAILORED_TEMPLATE = 'approved-skill-and-bullet-emphasis-v2'
TAILORED_MODEL = 'deterministic-skill-and-bullet-emphasis-v1'
SELECTION_TEMPLATE = 'approved-selection-and-bullet-emphasis-v2'
SELECTION_MODEL = 'deterministic-fact-selection-and-bullet-emphasis-v1'
SELECTION_TEMPLATES = {SELECTION_TEMPLATE, LEGACY_SELECTION_TEMPLATE, 'approved-fact-selection-headings-v1'}
EMPHASIS_TEMPLATES = {TAILORED_TEMPLATE, LEGACY_TAILORED_TEMPLATE, SELECTION_TEMPLATE, LEGACY_SELECTION_TEMPLATE}
RESPONSIBILITY_EMPHASIS_TEMPLATES = {TAILORED_TEMPLATE, SELECTION_TEMPLATE}
CONTACT_PAIRS = {
    BASE_TEMPLATE: ('approved-contact-base-v1', 'deterministic-contact-copy-v1'),
    TAILORED_TEMPLATE: ('approved-contact-skill-and-bullet-emphasis-v2', 'deterministic-contact-skill-and-bullet-emphasis-v1'),
    SELECTION_TEMPLATE: ('approved-contact-selection-and-bullet-emphasis-v2', 'deterministic-contact-selection-and-bullet-emphasis-v1'),
}
LEGACY_CONTACT_PAIRS = {
    LEGACY_TAILORED_TEMPLATE: ('approved-contact-emphasis-v1', 'deterministic-contact-order-v1'),
    LEGACY_SELECTION_TEMPLATE: ('approved-contact-selection-v1', 'deterministic-contact-selection-v1'),
}
CONTACT_TEMPLATES = {pair[0] for pair in CONTACT_PAIRS.values()}
CONTACT_TEMPLATES.update(pair[0] for pair in LEGACY_CONTACT_PAIRS.values())
SELECTION_TEMPLATES.add(CONTACT_PAIRS[SELECTION_TEMPLATE][0])
SELECTION_TEMPLATES.add(LEGACY_CONTACT_PAIRS[LEGACY_SELECTION_TEMPLATE][0])
EMPHASIS_TEMPLATES.update(pair[0] for pair in (*CONTACT_PAIRS.values(), *LEGACY_CONTACT_PAIRS.values()) if pair[0] != CONTACT_PAIRS[BASE_TEMPLATE][0])
RESPONSIBILITY_EMPHASIS_TEMPLATES.update(pair[0] for template,pair in CONTACT_PAIRS.items() if template in (TAILORED_TEMPLATE,SELECTION_TEMPLATE))
CONTRACTS = {
    'verified-facts-v1': ('deterministic-fact-copy-v1', 'BASE', False),
    'approved-skill-order-v1': ('deterministic-skill-order-v1', 'TAILORED', False),
    'approved-skill-order-headings-v1': ('deterministic-skill-order-v2', 'TAILORED', True),
    'approved-fact-selection-headings-v1': ('deterministic-fact-selection-v1', 'TAILORED', True),
    BASE_TEMPLATE: (BASE_MODEL, 'BASE', True),
    LEGACY_TAILORED_TEMPLATE: (LEGACY_TAILORED_MODEL, 'TAILORED', True),
    LEGACY_SELECTION_TEMPLATE: (LEGACY_SELECTION_MODEL, 'TAILORED', True),
    TAILORED_TEMPLATE: (TAILORED_MODEL, 'TAILORED', True),
    SELECTION_TEMPLATE: (SELECTION_MODEL, 'TAILORED', True),
}
CONTRACTS.update({pair[0]:(pair[1],CONTRACTS[source][1],True) for source,pair in CONTACT_PAIRS.items()})
CONTRACTS.update({pair[0]:(pair[1],CONTRACTS[source][1],True) for source,pair in LEGACY_CONTACT_PAIRS.items()})
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


def template_pair(tailored, selected, contact):
    template=SELECTION_TEMPLATE if selected else TAILORED_TEMPLATE if tailored else BASE_TEMPLATE
    return CONTACT_PAIRS[template] if contact else (template,CONTRACTS[template][0])


def contact_header(profile, template):
    lines=[profile['name'],profile['email']]
    if template not in CONTACT_TEMPLATES:return lines
    contact=profile.get('contact') or {}
    phones=[value.strip() for value in contact.get('phones',[]) if value.strip()]
    if phones:lines.append('Phone: '+' | '.join(phones))
    location=[contact.get(key,'').strip() for key in ('city','state','country') if contact.get(key,'').strip()]
    if location:lines.append('Location: '+', '.join(location))
    for key,label in (('linkedin','LinkedIn'),('github','GitHub'),('portfolio','Portfolio')):
        if contact.get(key):lines.append(label+': '+contact[key])
    return lines


def resume_layout(profile, facts, template):
    contract = CONTRACTS.get(template)
    if not contract:
        raise PreparationError('UNSUPPORTED_CLAIM', 'Document template has no assertion validation contract')
    paragraphs = contact_header(profile,template)
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
