"""Persist observed source quality without fabricating a historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = 'f083a019c241'
down_revision = 'd21b76ce903a'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('company_job_sources') as batch:
        batch.add_column(sa.Column('collector_source_id', sa.String(128), nullable=True))
        batch.create_foreign_key('fk_company_source_collector', 'job_sources', ['collector_source_id'], ['id'])
    op.create_table('source_quality_observations',
        sa.Column('id', sa.String(128), primary_key=True),
        sa.Column('configured_source_id', sa.String(128), sa.ForeignKey('job_sources.id')),
        sa.Column('company_source_id', sa.String(128), sa.ForeignKey('company_job_sources.id')),
        sa.Column('observed_at', sa.String(64), nullable=False),
        sa.Column('succeeded', sa.Boolean(), nullable=False), sa.Column('available', sa.Boolean()),
        sa.Column('enumerated', sa.Integer()), sa.Column('accepted', sa.Integer()),
        sa.Column('completeness_sum', sa.Float()), sa.Column('salary_known', sa.Integer()),
        sa.Column('links_checked', sa.Integer()), sa.Column('links_accurate', sa.Integer()),
        sa.Column('error_code', sa.String(100)),
        sa.CheckConstraint('(configured_source_id IS NOT NULL AND company_source_id IS NULL) OR '
                           '(configured_source_id IS NULL AND company_source_id IS NOT NULL)', name='quality_one_source'),
        sa.CheckConstraint('enumerated IS NULL OR enumerated >= 0', name='quality_enumerated_nonnegative'),
        sa.CheckConstraint('accepted IS NULL OR (enumerated IS NOT NULL AND accepted >= 0 AND accepted <= enumerated)', name='quality_accepted_bounds'),
        sa.CheckConstraint('completeness_sum IS NULL OR (accepted IS NOT NULL AND completeness_sum >= 0 AND completeness_sum <= accepted)', name='quality_completeness_bounds'),
        sa.CheckConstraint('salary_known IS NULL OR (accepted IS NOT NULL AND salary_known >= 0 AND salary_known <= accepted)', name='quality_salary_bounds'),
        sa.CheckConstraint('links_checked IS NULL OR (accepted IS NOT NULL AND links_checked >= 0 AND links_checked <= accepted)', name='quality_links_bounds'),
        sa.CheckConstraint('links_accurate IS NULL OR (links_checked IS NOT NULL AND links_accurate >= 0 AND links_accurate <= links_checked)', name='quality_accuracy_bounds'))
    op.create_index('ix_quality_configured_time', 'source_quality_observations', ['configured_source_id', 'observed_at'])
    op.create_index('ix_quality_company_time', 'source_quality_observations', ['company_source_id', 'observed_at'])


def downgrade():
    connection = op.get_bind()
    if (connection.execute(sa.text('SELECT 1 FROM source_quality_observations LIMIT 1')).first()
            or connection.execute(sa.text('SELECT 1 FROM company_job_sources WHERE collector_source_id IS NOT NULL LIMIT 1')).first()):
        raise RuntimeError('Refusing to discard source quality evidence; restore a verified backup for intentional rollback')
    op.drop_table('source_quality_observations')
    with op.batch_alter_table('company_job_sources') as batch:
        batch.drop_constraint('fk_company_source_collector', type_='foreignkey')
        batch.drop_column('collector_source_id')
