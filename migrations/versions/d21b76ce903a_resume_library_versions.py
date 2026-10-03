"""Preserve named base resume versions and backfill existing originals."""
from alembic import op
import sqlalchemy as sa

revision = 'd21b76ce903a'
down_revision = 'b71d9a42c0e3'
branch_labels = None
depends_on = None


def upgrade():
    revisions = op.create_table('resume_revisions',
        sa.Column('id', sa.String(128), primary_key=True),
        sa.Column('profile_id', sa.String(128), sa.ForeignKey('career_profiles.id'), nullable=False),
        sa.Column('upload_id', sa.String(128), sa.ForeignKey('uploaded_resumes.id'), nullable=False),
        sa.Column('series_id', sa.String(128), nullable=False),
        sa.Column('label', sa.String(200), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.String(64), nullable=False),
        sa.UniqueConstraint('profile_id','series_id','version'),
        sa.UniqueConstraint('profile_id','series_id','upload_id'))
    # Stable algorithm frozen here rather than importing mutable application code.
    import hashlib
    import json
    connection = op.get_bind()
    for row in connection.execute(sa.text('SELECT id, profile_id, filename, created_at FROM uploaded_resumes')).mappings():
        revision_id = hashlib.sha256(json.dumps(('resume-revision', row['profile_id'], row['id'], row['id']),
                                               ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        connection.execute(revisions.insert().values(id=revision_id, profile_id=row['profile_id'], upload_id=row['id'],
            series_id=row['id'], label=row['filename'], version=1, created_at=row['created_at']))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM resume_revisions LIMIT 1')).first():
        raise RuntimeError('Refusing to discard resume version history; restore a verified backup for intentional rollback')
    op.drop_table('resume_revisions')
