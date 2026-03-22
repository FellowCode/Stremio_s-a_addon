"""add anime release status cache

Revision ID: 0005_add_anime_release_status_cache
Revises: 0004_add_cinemeta_title_cache
Create Date: 2026-03-18 01:35:00

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0005_add_anime_release_status_cache'
down_revision = '0004_add_cinemeta_title_cache'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'anime_release_status_cache',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('normalized_title', sa.String(length=255), nullable=False),
        sa.Column('is_ongoing', sa.Boolean(), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('normalized_title', name='uq_anime_release_status_cache_normalized_title'),
    )
    op.create_index(op.f('ix_anime_release_status_cache_normalized_title'), 'anime_release_status_cache', ['normalized_title'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_anime_release_status_cache_normalized_title'), table_name='anime_release_status_cache')
    op.drop_table('anime_release_status_cache')
