from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
	pass


class CacheTimestampMixin:
	created_at: Mapped[datetime] = mapped_column(
		DateTime(timezone=True),
		server_default=func.now(),
		nullable=False,
	)
	updated_at: Mapped[datetime] = mapped_column(
		DateTime(timezone=True),
		server_default=func.now(),
		onupdate=func.now(),
		nullable=False,
	)


class SmotretAnimeEpisodeCache(CacheTimestampMixin, Base):
	__tablename__ = 'sa_episode_cache'
	__table_args__ = (
		UniqueConstraint('anime_title', 'episode_number', name='uq_sa_episode_cache_title_episode'),
	)

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	anime_title: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
	episode_number: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
	episode_url: Mapped[str] = mapped_column(Text, nullable=False)

	translations: Mapped[list[SmotretAnimeTranslationCache]] = relationship(
		back_populates='episode',
		cascade='all, delete-orphan',
		passive_deletes=True,
	)


class SmotretAnimeTranslationCache(CacheTimestampMixin, Base):
	__tablename__ = 'sa_translation_cache'
	__table_args__ = (
		UniqueConstraint(
			'episode_cache_id',
			'translation_type',
			'translation_url',
			name='uq_sa_translation_cache_episode_type_url',
		),
	)

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	episode_cache_id: Mapped[int] = mapped_column(
		ForeignKey('sa_episode_cache.id', ondelete='CASCADE'),
		index=True,
		nullable=False,
	)
	translation_type: Mapped[str] = mapped_column(String(8), index=True, nullable=False)
	translation_name: Mapped[str] = mapped_column(String(255), nullable=False)
	translation_url: Mapped[str] = mapped_column(Text, nullable=False)

	episode: Mapped[SmotretAnimeEpisodeCache] = relationship(back_populates='translations')


class SmotretAnimeVideoCache(CacheTimestampMixin, Base):
	__tablename__ = 'sa_video_cache'
	__table_args__ = (
		UniqueConstraint('translation_url', 'quality_profile', name='uq_sa_video_cache_translation_url_quality_profile'),
	)

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	translation_url: Mapped[str] = mapped_column(Text, nullable=False, index=True)
	quality_profile: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
	video_url: Mapped[str] = mapped_column(Text, nullable=False)


class AniListTitleCache(CacheTimestampMixin, Base):
	__tablename__ = 'anilist_title_cache'
	__table_args__ = (
		UniqueConstraint('content_type', 'seed_title', name='uq_anilist_title_cache_content_type_seed_title'),
	)

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	content_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
	seed_title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
	titles_json: Mapped[str] = mapped_column(Text, nullable=False)


class CinemetaTitleCache(CacheTimestampMixin, Base):
	__tablename__ = 'cinemeta_title_cache'
	__table_args__ = (
		UniqueConstraint('content_type', 'stremio_id', name='uq_cinemeta_title_cache_content_type_stremio_id'),
	)

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	content_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
	stremio_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
	titles_json: Mapped[str] = mapped_column(Text, nullable=False)


class KitsuMappingsCache(CacheTimestampMixin, Base):
	__tablename__ = 'kitsu_mappings_cache'
	__table_args__ = (
		UniqueConstraint('kitsu_id', name='uq_kitsu_mappings_cache_kitsu_id'),
	)

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	kitsu_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
	external_urls_json: Mapped[str] = mapped_column(Text, nullable=False)


class AnimeReleaseStatusCache(CacheTimestampMixin, Base):
	__tablename__ = 'anime_release_status_cache'
	__table_args__ = (
		UniqueConstraint('normalized_title', name='uq_anime_release_status_cache_normalized_title'),
	)

	id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
	normalized_title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
	is_ongoing: Mapped[bool] = mapped_column(Boolean, nullable=False)
	finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
