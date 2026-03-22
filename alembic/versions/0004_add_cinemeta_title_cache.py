"""add cinemeta title cache

Revision ID: 0004_add_cinemeta_title_cache
Revises: 0003_add_anilist_title_cache
Create Date: 2026-03-18 01:15:00

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0004_add_cinemeta_title_cache'
down_revision = '0003_add_anilist_title_cache'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cinemeta_title_cache',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('content_type', sa.String(length=16), nullable=False),
        sa.Column('stremio_id', sa.String(length=32), nullable=False),
        sa.Column('titles_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('content_type', 'stremio_id', name='uq_cinemeta_title_cache_content_type_stremio_id'),
    )
    op.create_index(op.f('ix_cinemeta_title_cache_content_type'), 'cinemeta_title_cache', ['content_type'], unique=False)
    op.create_index(op.f('ix_cinemeta_title_cache_stremio_id'), 'cinemeta_title_cache', ['stremio_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_cinemeta_title_cache_stremio_id'), table_name='cinemeta_title_cache')
    op.drop_index(op.f('ix_cinemeta_title_cache_content_type'), table_name='cinemeta_title_cache')
    op.drop_table('cinemeta_title_cache')
