"""Provenance-only links between reviewed answer exports and approved stories."""
from __future__ import annotations

from sqlalchemy import select

from packages.persistence import models as m
from services.career_profile.story_bank import StoryBankError, list_stories
from services.documents.answers import _SENSITIVE_TERMS


class AnswerStoryCitationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_answer_story_citations(store, profile_id: str, items, citations) -> list[dict]:
    """Revalidate an explicitly selected citation against its exact answer and story revision."""
    if not citations:
        return []
    try:
        stories = list_stories(store, profile_id)
    except StoryBankError as error:
        raise AnswerStoryCitationError("APPLICATION_ANSWER_STORY_CITATION_NOT_CURRENT") from error
    by_identity = {
        (row.get("story_id"), row.get("revision")): row
        for row in stories
        if row.get("is_latest") is True
        and row.get("review_status") == "APPROVED"
        and row.get("reusable") is True
        and "APPLICATION_ANSWER" in row.get("allowed_uses", [])
    }
    clean = []
    seen = set()
    for citation in citations:
        item_index = citation.item_index
        identity = (item_index, citation.story_id, citation.revision)
        if identity in seen:
            raise AnswerStoryCitationError("APPLICATION_ANSWER_STORY_CITATION_INVALID")
        seen.add(identity)
        if item_index >= len(items):
            raise AnswerStoryCitationError("APPLICATION_ANSWER_STORY_CITATION_INVALID")
        question = items[item_index].question.strip()
        if _SENSITIVE_TERMS.search(question):
            raise AnswerStoryCitationError(
                "APPLICATION_ANSWER_STORY_CITATION_SENSITIVE_QUESTION")
        story = by_identity.get((citation.story_id, citation.revision))
        if story is None:
            raise AnswerStoryCitationError("APPLICATION_ANSWER_STORY_CITATION_NOT_CURRENT")
        expected = [
            {"fact_id": row["fact_id"], "fingerprint": row["fingerprint"]}
            for row in story.get("fact_fingerprints", [])
            if isinstance(row, dict)
            and isinstance(row.get("fact_id"), str)
            and isinstance(row.get("fingerprint"), str)
        ]
        supplied = [
            {"fact_id": row.fact_id, "fingerprint": row.fingerprint}
            for row in citation.fact_provenance
        ]
        if not expected or supplied != expected:
            raise AnswerStoryCitationError("APPLICATION_ANSWER_STORY_CITATION_NOT_CURRENT")
        clean.append({
            "item_index": item_index,
            "story_id": citation.story_id,
            "revision": citation.revision,
            "fact_provenance": expected,
        })
    return clean


def record_answer_story_citations(store, *, application_id: str, document_id: str,
                                  document_version: int, citations: list[dict]) -> None:
    """Append citation identities to application audit history without changing export bytes."""
    if not citations:
        return
    with store.engine.begin() as connection:
        store._audit(connection, application_id,
                     "APPLICATION_ANSWER_STORY_CITATIONS_RECORDED", {
                         "document_id": document_id,
                         "document_version": document_version,
                         "citations": citations,
                     })


def answer_story_citations_by_document(connection, application_id: str) -> dict[str, list[dict]]:
    """Read only citation identities for one application's saved exports."""
    events = connection.execute(select(m.audit.c.payload, m.audit.c.timestamp).where(
        m.audit.c.entity_id == application_id,
        m.audit.c.action == "APPLICATION_ANSWER_STORY_CITATIONS_RECORDED",
    ).order_by(m.audit.c.timestamp, m.audit.c.id)).mappings()
    result = {}
    for event in events:
        payload = event["payload"]
        document_id = payload.get("document_id") if isinstance(payload, dict) else None
        citations = payload.get("citations") if isinstance(payload, dict) else None
        if not isinstance(document_id, str) or not isinstance(citations, list):
            continue
        clean = []
        for citation in citations:
            if (not isinstance(citation, dict)
                    or set(citation) != {"item_index", "story_id", "revision", "fact_provenance"}
                    or type(citation.get("item_index")) is not int
                    or citation["item_index"] < 0
                    or not isinstance(citation.get("story_id"), str)
                    or type(citation.get("revision")) is not int
                    or citation["revision"] < 1
                    or not isinstance(citation.get("fact_provenance"), list)):
                clean = []
                break
            facts = citation["fact_provenance"]
            if (not facts or any(
                    not isinstance(fact, dict)
                    or set(fact) != {"fact_id", "fingerprint"}
                    or not isinstance(fact.get("fact_id"), str)
                    or not isinstance(fact.get("fingerprint"), str)
                    for fact in facts)):
                clean = []
                break
            clean.append({
                "item_index": citation["item_index"],
                "story_id": citation["story_id"],
                "revision": citation["revision"],
                "fact_provenance": [dict(fact) for fact in facts],
            })
        if clean:
            result[document_id] = clean
    return result
