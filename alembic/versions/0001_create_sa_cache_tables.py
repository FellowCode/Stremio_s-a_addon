"""create sa cache tables

Revision ID: 0001_create_sa_cache_tables
Revises:
Create Date: 2026-03-18 00:00:00

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0001_create_sa_cache_tables'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'sa_episode_cache',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('anime_title', sa.String(length=255), nullable=False),
        sa.Column('episode_number', sa.Integer(), nullable=False),
        sa.Column('episode_url', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('anime_title', 'episode_number', name='uq_sa_episode_cache_title_episode'),
    )
    op.create_index(op.f('ix_sa_episode_cache_anime_title'), 'sa_episode_cache', ['anime_title'], unique=False)
    op.create_index(op.f('ix_sa_episode_cache_episode_number'), 'sa_episode_cache', ['episode_number'], unique=False)

    op.create_table(
        'sa_translation_cache',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('episode_cache_id', sa.Integer(), nullable=False),
        sa.Column('translation_type', sa.String(length=8), nullable=False),
        sa.Column('translation_name', sa.String(length=255), nullable=False),
        sa.Column('translation_url', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['episode_cache_id'], ['sa_episode_cache.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'episode_cache_id',
            'translation_type',
            'translation_url',
            name='uq_sa_translation_cache_episode_type_url',
        ),
    )
    op.create_index(op.f('ix_sa_translation_cache_episode_cache_id'), 'sa_translation_cache', ['episode_cache_id'], unique=False)
    op.create_index(op.f('ix_sa_translation_cache_translation_type'), 'sa_translation_cache', ['translation_type'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_sa_translation_cache_translation_type'), table_name='sa_translation_cache')
    op.drop_index(op.f('ix_sa_translation_cache_episode_cache_id'), table_name='sa_translation_cache')
    op.drop_table('sa_translation_cache')

    op.drop_index(op.f('ix_sa_episode_cache_episode_number'), table_name='sa_episode_cache')
    op.drop_index(op.f('ix_sa_episode_cache_anime_title'), table_name='sa_episode_cache')
    op.drop_table('sa_episode_cache')