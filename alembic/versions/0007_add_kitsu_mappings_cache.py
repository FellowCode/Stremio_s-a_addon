"""add kitsu mappings cache

Revision ID: 0007_add_kitsu_mappings_cache
Revises: 0006_split_video_cache_by_quality_profile
Create Date: 2026-03-18 14:05:00

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0007_add_kitsu_mappings_cache'
down_revision = '0006_split_video_cache_by_quality_profile'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'kitsu_mappings_cache',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('kitsu_id', sa.Integer(), nullable=False),
        sa.Column('external_urls_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kitsu_id', name='uq_kitsu_mappings_cache_kitsu_id'),
    )
    op.create_index(op.f('ix_kitsu_mappings_cache_kitsu_id'), 'kitsu_mappings_cache', ['kitsu_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_kitsu_mappings_cache_kitsu_id'), table_name='kitsu_mappings_cache')
    op.drop_table('kitsu_mappings_cache')