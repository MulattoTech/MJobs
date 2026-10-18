"""Add conservative reviewed company-monitor policy and durable rate cursor."""
from alembic import op
import sqlalchemy as sa

revision = '20261018cm01'
down_revision = '20261014rh01'
branch_labels = None
depends_on = None

LIMITS = (('request_timeout', 10, 2, 10), ('rate_limit', 60, 1, 60),
          ('concurrency', 1, 1, 8), ('jitter_seconds', 0, 0, 300))


def upgrade():
    with op.batch_alter_table('company_source_monitoring') as batch:
        for key, default, low, high in LIMITS:
            batch.add_column(sa.Column(key, sa.Integer(), nullable=False, server_default=str(default)))
            batch.create_check_constraint('company_source_monitoring_' + key + '_valid',
                                          f'{key} BETWEEN {low} AND {high}')
        batch.add_column(sa.Column('request_not_before', sa.String(64)))


def downgrade():
    connection = op.get_bind()
    changed = ' OR '.join(f'{key} <> {default}' for key, default, _, _ in LIMITS)
    if connection.execute(sa.text('SELECT COUNT(*) FROM company_source_monitoring WHERE '
                                  + changed + ' OR request_not_before IS NOT NULL')).scalar_one():
        raise RuntimeError('Refusing to discard company monitoring policy or rate state')
    with op.batch_alter_table('company_source_monitoring') as batch:
        for key, _, _, _ in LIMITS:
            batch.drop_constraint('company_source_monitoring_' + key + '_valid', type_='check')
            batch.drop_column(key)
        batch.drop_column('request_not_before')
