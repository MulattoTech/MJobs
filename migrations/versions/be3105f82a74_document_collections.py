"""Classify immutable document collections while preserving original blobs."""
from alembic import op
import sqlalchemy as sa

revision = 'be3105f82a74'
down_revision = 'af2049e17b63'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('resume_revisions') as batch:
        batch.add_column(sa.Column('document_kind', sa.String(32), nullable=False, server_default='BASE_RESUME'))
        batch.create_check_constraint('ck_resume_revision_kind',
            "document_kind IN ('BASE_RESUME','MASTER_RESUME','TAILORED_RESUME','COVER_LETTER','APPLICATION_ANSWER_EXPORT','SUPPORTING_DOCUMENT','CERTIFICATION')")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM resume_revisions WHERE document_kind <> 'BASE_RESUME' LIMIT 1")).first():
        raise RuntimeError('Refusing to discard document collection types; restore a verified backup for intentional rollback')
    with op.batch_alter_table('resume_revisions') as batch:
        batch.drop_constraint('ck_resume_revision_kind', type_='check')
        batch.drop_column('document_kind')
