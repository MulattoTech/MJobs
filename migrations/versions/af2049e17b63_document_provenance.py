"""Record generation provenance without inventing it for historical documents."""
from alembic import op
import sqlalchemy as sa

revision = 'af2049e17b63'
down_revision = 'e9024a1b7c30'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('documents', sa.Column('created_at', sa.String(64)))
    op.add_column('documents', sa.Column('generation_model', sa.String(128)))


def downgrade():
    if op.get_bind().execute(sa.text(
        'SELECT 1 FROM documents WHERE created_at IS NOT NULL OR generation_model IS NOT NULL LIMIT 1'
    )).first():
        raise RuntimeError('Refusing to discard document generation provenance; restore a verified backup for intentional rollback')
    with op.batch_alter_table('documents') as batch:
        batch.drop_column('generation_model')
        batch.drop_column('created_at')
