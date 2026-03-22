"""add anilist title cache

Revision ID: 0003_add_anilist_title_cache
Revises: 0002_add_sa_video_cache
Create Date: 2026-03-18 01:00:00

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0003_add_anilist_title_cache'
down_revision = '0002_add_sa_video_cache'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'anilist_title_cache',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('content_type', sa.String(length=16), nullable=False),
        sa.Column('seed_title', sa.String(length=255), nullable=False),
        sa.Column('titles_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('content_type', 'seed_title', name='uq_anilist_title_cache_content_type_seed_title'),
    )
    op.create_index(op.f('ix_anilist_title_cache_content_type'), 'anilist_title_cache', ['content_type'], unique=False)
    op.create_index(op.f('ix_anilist_title_cache_seed_title'), 'anilist_title_cache', ['seed_title'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_anilist_title_cache_seed_title'), table_name='anilist_title_cache')
    op.drop_index(op.f('ix_anilist_title_cache_content_type'), table_name='anilist_title_cache')
    op.drop_table('anilist_title_cache')
