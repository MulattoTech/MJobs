"""Deterministic whole-term exclusions for profile-generated career content."""
import unicodedata

from services.documents.errors import PreparationError


def _normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _word_character(value):
    category = unicodedata.category(value)
    return category[0] in {'L', 'M', 'N'} or category == 'Pc'


def _contains_whole_term(content, term):
    start = 0
    while True:
        index = content.find(term, start)
        if index < 0:
            return False
        end = index + len(term)
        before_is_word = index > 0 and _word_character(content[index - 1])
        after_is_word = end < len(content) and _word_character(content[end])
        if not before_is_word and not after_is_word:
            return True
        start = index + 1


def find_avoided_term(content, terms):
    normalized_content = _normalized(content)
    for configured in terms or []:
        if not isinstance(configured, str):
            continue
        term = _normalized(configured)
        if term and _contains_whole_term(normalized_content, term):
            return configured.strip()
    return None


def validate_content(content, preferences, output_path):
    preferences = preferences if isinstance(preferences, dict) else {}
    configured = preferences.get('avoid_terms', [])
    term = find_avoided_term(content, configured if isinstance(configured, list) else [])
    if term is not None:
        display_term = ' '.join(term.split()).replace('"', "'")
        raise PreparationError(
            'AVOID_LIST_BLOCKED',
            f'Configured avoid term "{display_term}" blocks generated {output_path} content')
