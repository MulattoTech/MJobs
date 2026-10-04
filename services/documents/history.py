"""Read document evidence from its original immutable profile snapshot."""
from sqlalchemy import select
from packages.persistence import models as m


def application_documents(store, application_id):
    application = store.application(application_id)
    with store.engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(select(m.documents).where(
            m.documents.c.application_id == application_id).order_by(
            m.documents.c.version.desc(), m.documents.c.kind)).mappings()]
        facts = {}
        versions = {row['profile_version'] for row in rows}
        if versions:
            for fact in connection.execute(select(m.facts).where(
                m.facts.c.profile_id == application['profile_id'], m.facts.c.version.in_(versions)
            )).mappings():
                facts[(fact['version'], fact['fact_id'])] = dict(fact)
        for row in rows:
            row['job_id'] = application['job_id']
            row['profile_id'] = application['profile_id']
            row['facts_used'] = [facts[(row['profile_version'], identifier)] for identifier in row['fact_ids']
                                 if (row['profile_version'], identifier) in facts]
            row['missing_fact_ids'] = [identifier for identifier in row['fact_ids']
                                       if (row['profile_version'], identifier) not in facts]
        return rows
