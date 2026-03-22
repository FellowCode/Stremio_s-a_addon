"""add sa video cache

Revision ID: 0002_add_sa_video_cache
Revises: 0001_create_sa_cache_tables
Create Date: 2026-03-18 00:30:00

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0002_add_sa_video_cache'
down_revision = '0001_create_sa_cache_tables'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'sa_video_cache',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('translation_url', sa.Text(), nullable=False),
        sa.Column('video_url', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('translation_url', name='uq_sa_video_cache_translation_url'),
    )
    op.create_index(op.f('ix_sa_video_cache_translation_url'), 'sa_video_cache', ['translation_url'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_sa_video_cache_translation_url'), table_name='sa_video_cache')
    op.drop_table('sa_video_cache')