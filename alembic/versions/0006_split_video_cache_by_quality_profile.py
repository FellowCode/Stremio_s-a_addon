"""split video cache by quality profile

Revision ID: 0006_split_video_cache_by_quality_profile
Revises: 0005_add_anime_release_status_cache
Create Date: 2026-03-18 02:00:00

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0006_split_video_cache_by_quality_profile'
down_revision = '0005_add_anime_release_status_cache'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text('DROP TABLE IF EXISTS _alembic_tmp_sa_video_cache'))
    inspector = inspect(bind)
    columns = {column['name'] for column in inspector.get_columns('sa_video_cache')}
    unique_constraints = {constraint['name'] for constraint in inspector.get_unique_constraints('sa_video_cache')}

    with op.batch_alter_table('sa_video_cache', recreate='always') as batch_op:
        if 'quality_profile' not in columns:
            batch_op.add_column(sa.Column('quality_profile', sa.String(length=16), nullable=False, server_default='fhd'))

        if 'uq_sa_video_cache_translation_url' in unique_constraints:
            batch_op.drop_constraint('uq_sa_video_cache_translation_url', type_='unique')

        if 'uq_sa_video_cache_translation_url_quality_profile' not in unique_constraints:
            batch_op.create_unique_constraint(
                'uq_sa_video_cache_translation_url_quality_profile',
                ['translation_url', 'quality_profile'],
            )

    inspector = inspect(bind)
    indexes = {index['name'] for index in inspector.get_indexes('sa_video_cache')}
    if 'ix_sa_video_cache_quality_profile' not in indexes:
        op.create_index(op.f('ix_sa_video_cache_quality_profile'), 'sa_video_cache', ['quality_profile'], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text('DROP TABLE IF EXISTS _alembic_tmp_sa_video_cache'))
    inspector = inspect(bind)
    unique_constraints = {constraint['name'] for constraint in inspector.get_unique_constraints('sa_video_cache')}

    indexes = {index['name'] for index in inspector.get_indexes('sa_video_cache')}
    if 'ix_sa_video_cache_quality_profile' in indexes:
        op.drop_index(op.f('ix_sa_video_cache_quality_profile'), table_name='sa_video_cache')

    with op.batch_alter_table('sa_video_cache', recreate='always') as batch_op:
        if 'uq_sa_video_cache_translation_url_quality_profile' in unique_constraints:
            batch_op.drop_constraint('uq_sa_video_cache_translation_url_quality_profile', type_='unique')
        if 'uq_sa_video_cache_translation_url' not in unique_constraints:
            batch_op.create_unique_constraint('uq_sa_video_cache_translation_url', ['translation_url'])
        batch_op.drop_column('quality_profile')
