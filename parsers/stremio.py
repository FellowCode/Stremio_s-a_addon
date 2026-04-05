from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import re

import httpx
from sqlalchemy import select

from db.models import AniListTitleCache, CinemetaTitleCache, KitsuMappingsCache
from db.session import get_session
from parsers.kitsu import get_anime, get_anime_search_titles
from settings import ANILIST_CACHE_TTL_HOURS, CINEMETA_CACHE_TTL_HOURS, KITSU_MAPPINGS_CACHE_TTL_HOURS


logger = logging.getLogger(__name__)


ANILIST_SEARCH_QUERY = '''
query ($search: String!) {
    Page(page: 1, perPage: 8) {
        media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
            format
            seasonYear
            startDate {
                year
            }
            title {
                romaji
                english
                native
            }
            synonyms
        }
    }
}
'''

ANILIST_CACHE_TTL = timedelta(hours=ANILIST_CACHE_TTL_HOURS)
CINEMETA_CACHE_TTL = timedelta(hours=CINEMETA_CACHE_TTL_HOURS)
KITSU_MAPPINGS_CACHE_TTL = timedelta(hours=KITSU_MAPPINGS_CACHE_TTL_HOURS)
SEARCH_TARGETS_CACHE_TTL = timedelta(hours=12)
_SEARCH_TARGETS_CACHE: dict[tuple[str, str, str, int | None, int], tuple[datetime, SearchTargets]] = {}


ANILIST_MEDIA_BY_ID_QUERY = '''
query ($id: Int!) {
    Media(id: $id, type: ANIME) {
        idMal
        title {
            romaji
            english
            native
        }
        synonyms
    }
}
'''

ANILIST_MEDIA_BY_MAL_ID_QUERY = '''
query ($idMal: Int!) {
    Media(idMal: $idMal, type: ANIME) {
        title {
            english
            romaji
            native
        }
        synonyms
    }
}
'''


@dataclass(slots=True)
class StreamRequest:
    provider: str
    catalog_id: str
    episode_number: int
    season_number: int | None = None


@dataclass(slots=True)
class SearchTargets:
    titles: list[str]
    external_urls: list[str]


def parse_stream_request_id(content_type: str, raw_id: str) -> StreamRequest:
    parts = raw_id.split(':')
    if not parts:
        raise ValueError('Invalid stream id')

    provider: str
    catalog_id: str
    remainder: list[str]

    if parts[0] in {'kitsu', 'anilist'} and len(parts) >= 2:
        provider = parts[0]
        catalog_id = parts[1]
        remainder = parts[2:]
    elif parts[0].startswith('tt'):
        provider = 'tt'
        catalog_id = parts[0]
        remainder = parts[1:]
    else:
        raise ValueError(f'Unsupported stream id: {raw_id}')

    season_number: int | None = None
    episode_number = 1

    if content_type != 'movie':
        if len(remainder) >= 2:
            season_number = int(remainder[0])
            episode_number = int(remainder[1])
        elif len(remainder) == 1:
            episode_number = int(remainder[0])

    return StreamRequest(
        provider=provider,
        catalog_id=catalog_id,
        episode_number=episode_number,
        season_number=season_number,
    )


async def get_stream_search_targets(content_type: str, stream_request: StreamRequest) -> SearchTargets:
    cache_key = (
        content_type,
        stream_request.provider,
        stream_request.catalog_id,
        stream_request.season_number,
        stream_request.episode_number,
    )
    cached_targets = _get_cached_search_targets(cache_key)
    if cached_targets is not None:
        if stream_request.provider != 'kitsu' or cached_targets.external_urls:
            return cached_targets
        _SEARCH_TARGETS_CACHE.pop(cache_key, None)

    if stream_request.provider == 'kitsu':
        anime, external_urls = await _get_kitsu_search_data(int(stream_request.catalog_id))
        base_titles = get_anime_search_titles(anime)
        enriched_titles = await _get_kitsu_enriched_titles(external_urls)
        targets = SearchTargets(
            titles=_merge_search_titles(base_titles, enriched_titles, stream_request.season_number),
            external_urls=external_urls,
        )
        _save_cached_search_targets(cache_key, targets)
        return targets

    if stream_request.provider == 'anilist':
        titles, external_urls = await get_anilist_search_targets(int(stream_request.catalog_id))
        targets = SearchTargets(titles=titles, external_urls=external_urls)
        _save_cached_search_targets(cache_key, targets)
        return targets

    if stream_request.provider == 'tt':
        targets = SearchTargets(
            titles=await get_cinemeta_search_titles(
                content_type,
                stream_request.catalog_id,
                season_number=stream_request.season_number,
                episode_number=stream_request.episode_number,
            ),
            external_urls=[],
        )
        _save_cached_search_targets(cache_key, targets)
        return targets

    raise ValueError(f'Unsupported provider: {stream_request.provider}')


async def get_anilist_search_targets(anilist_id: int) -> tuple[list[str], list[str]]:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.post(
            'https://graphql.anilist.co',
            json={'query': ANILIST_MEDIA_BY_ID_QUERY, 'variables': {'id': anilist_id}},
            headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
        )

    if response.status_code != 200:
        return [], []

    media = response.json().get('data', {}).get('Media') or {}
    titles: list[str] = []
    seen_titles: set[str] = set()

    for value in (
        (media.get('title') or {}).get('english'),
        (media.get('title') or {}).get('romaji'),
        (media.get('title') or {}).get('native'),
    ):
        _append_title_variants(titles, seen_titles, value)

    synonyms = media.get('synonyms') or []
    if isinstance(synonyms, list):
        for value in synonyms:
            _append_title_variants(titles, seen_titles, value)

    external_urls: list[str] = []
    seen_urls: set[str] = set()
    mal_id = media.get('idMal')
    if isinstance(mal_id, int) and mal_id > 0:
        _append_external_search_url(external_urls, seen_urls, f'https://myanimelist.net/anime/{mal_id}')

    return titles, external_urls


async def _get_kitsu_enriched_titles(external_urls: list[str]) -> list[str]:
    mal_id = _extract_mal_anime_id(external_urls)
    if mal_id is None:
        return []

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.post(
            'https://graphql.anilist.co',
            json={'query': ANILIST_MEDIA_BY_MAL_ID_QUERY, 'variables': {'idMal': mal_id}},
            headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
        )

    if response.status_code != 200:
        return []

    media = response.json().get('data', {}).get('Media') or {}
    titles: list[str] = []
    seen_titles: set[str] = set()

    for value in (
        (media.get('title') or {}).get('english'),
        (media.get('title') or {}).get('romaji'),
        (media.get('title') or {}).get('native'),
    ):
        _append_title_variants(titles, seen_titles, value)

    synonyms = media.get('synonyms') or []
    if isinstance(synonyms, list):
        for value in synonyms:
            _append_title_variants(titles, seen_titles, value)

    return titles


async def _get_kitsu_search_data(kitsu_id: int):
    return await asyncio.gather(
        get_anime(kitsu_id),
        _get_kitsu_external_search_urls(kitsu_id),
    )


async def _get_kitsu_external_search_urls(kitsu_id: int) -> list[str]:
    cached_urls = await _get_cached_kitsu_external_search_urls(kitsu_id)
    if cached_urls is not None:
        return cached_urls

    url = f'https://kitsu.io/api/edge/anime/{kitsu_id}/mappings'
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(url, headers={'Accept': 'application/vnd.api+json'})

    if response.status_code != 200:
        return []

    payload = response.json().get('data') or []
    external_urls: list[str] = []
    seen_urls: set[str] = set()

    for item in payload:
        attributes = item.get('attributes') or {}
        external_site = attributes.get('externalSite')
        external_id = attributes.get('externalId')
        if not isinstance(external_site, str) or not isinstance(external_id, str):
            continue

        external_url = _build_supported_external_url(external_site, external_id)
        if external_url is None:
            continue
        _append_external_search_url(external_urls, seen_urls, external_url)

    await _save_cached_kitsu_external_search_urls(kitsu_id, external_urls)
    return external_urls


async def _get_cached_kitsu_external_search_urls(kitsu_id: int) -> list[str] | None:
    async with get_session() as session:
        statement = select(KitsuMappingsCache).where(KitsuMappingsCache.kitsu_id == kitsu_id)
        cached_entry = await session.scalar(statement)
        if cached_entry is None or not _is_cache_fresh(cached_entry.updated_at, cached_entry.created_at, KITSU_MAPPINGS_CACHE_TTL):
            return None

        try:
            payload = json.loads(cached_entry.external_urls_json)
        except json.JSONDecodeError:
            logger.warning('Failed to decode cached Kitsu external URLs: kitsu_id=%s', kitsu_id, exc_info=True)
            return None

        if not isinstance(payload, list):
            return None

        return [value for value in payload if isinstance(value, str) and value.strip()]


async def _save_cached_kitsu_external_search_urls(kitsu_id: int, external_urls: list[str]) -> None:
    payload = [value for value in external_urls if isinstance(value, str) and value.strip()]

    async with get_session() as session:
        statement = select(KitsuMappingsCache).where(KitsuMappingsCache.kitsu_id == kitsu_id)
        cached_entry = await session.scalar(statement)

        if cached_entry is None:
            session.add(
                KitsuMappingsCache(
                    kitsu_id=kitsu_id,
                    external_urls_json=json.dumps(payload, ensure_ascii=False),
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                )
            )
        else:
            cached_entry.external_urls_json = json.dumps(payload, ensure_ascii=False)
            cached_entry.updated_at = _utcnow()

        await session.commit()


def _build_supported_external_url(external_site: str, external_id: str) -> str | None:
    normalized_site = external_site.casefold().strip()

    if normalized_site == 'myanimelist/anime':
        return f'https://myanimelist.net/anime/{external_id}'
    if normalized_site in {'anidb', 'anidb/anime'}:
        return f'https://anidb.net/anime/{external_id}'
    if normalized_site in {'shikimori', 'shikimori/anime'}:
        return f'https://shikimori.one/animes/{external_id}'
    if normalized_site in {'world-art', 'worldart', 'world-art.ru', 'world-art/anime'}:
        return f'https://world-art.ru/animation/animation.php?id={external_id}'
    if normalized_site in {'animenewsnetwork', 'animenewsnetwork/anime', 'anime-news-network'}:
        return f'https://www.animenewsnetwork.com/encyclopedia/anime.php?id={external_id}'
    if normalized_site in {'fansubs', 'fansubs.ru', 'kage-project', 'kageproject'}:
        return f'https://fansubs.ru/base.php?id={external_id}'
    return None


def _extract_mal_anime_id(external_urls: list[str]) -> int | None:
    for url in external_urls:
        match = re.search(r'myanimelist\.net/anime/(\d+)', url, flags=re.IGNORECASE)
        if match is None:
            continue
        return int(match.group(1))
    return None


def _append_external_search_url(urls: list[str], seen: set[str], value: str | None) -> None:
    if not isinstance(value, str):
        return
    normalized = value.strip()
    if not normalized:
        return
    key = normalized.casefold()
    if key in seen:
        return
    seen.add(key)
    urls.append(normalized)


def _get_cached_search_targets(cache_key: tuple[str, str, str, int | None, int]) -> SearchTargets | None:
    cached_entry = _SEARCH_TARGETS_CACHE.get(cache_key)
    if cached_entry is None:
        return None
    cached_at, cached_targets = cached_entry
    if _utcnow() - cached_at > SEARCH_TARGETS_CACHE_TTL:
        _SEARCH_TARGETS_CACHE.pop(cache_key, None)
        return None
    return SearchTargets(
        titles=list(cached_targets.titles),
        external_urls=list(cached_targets.external_urls),
    )


def _save_cached_search_targets(cache_key: tuple[str, str, str, int | None, int], search_targets: SearchTargets) -> None:
    _SEARCH_TARGETS_CACHE[cache_key] = (
        _utcnow(),
        SearchTargets(
            titles=list(search_targets.titles),
            external_urls=list(search_targets.external_urls),
        ),
    )


async def get_cinemeta_search_titles(
    content_type: str,
    stremio_id: str,
    season_number: int | None = None,
    episode_number: int = 1,
) -> list[str]:
    cached_titles = await _get_cached_cinemeta_titles(content_type, stremio_id)
    if cached_titles is not None:
        target_year = await _get_cinemeta_target_year(content_type, stremio_id, season_number, episode_number)
        enriched_titles = await _get_anilist_search_titles(content_type, list(cached_titles), target_year=target_year)
        return _merge_search_titles(list(cached_titles), enriched_titles, season_number)

    meta = await _fetch_cinemeta_meta(content_type, stremio_id)
    if meta is None:
        return []

    titles = _extract_cinemeta_titles(meta)
    await _save_cached_cinemeta_titles(content_type, stremio_id, titles)

    target_year = _extract_cinemeta_target_year(meta, season_number, episode_number)
    enriched_titles = await _get_anilist_search_titles(content_type, titles, target_year=target_year)
    return _merge_search_titles(titles, enriched_titles, season_number)


async def _fetch_cinemeta_meta(content_type: str, stremio_id: str) -> dict | None:
    url = f'https://v3-cinemeta.strem.io/meta/{content_type}/{stremio_id}.json'
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(url)

    if response.status_code != 200:
        return None

    meta = response.json().get('meta') or {}
    return meta if isinstance(meta, dict) else None


def _extract_cinemeta_titles(meta: dict) -> list[str]:
    titles: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        _append_title_variants(titles, seen, value)

    add(meta.get('name'))

    slug = meta.get('slug')
    if isinstance(slug, str):
        slug_tail = slug.split('/')[-1]
        if '-' in slug_tail:
            slug_tail = slug_tail.rsplit('-', 1)[0]
        add(slug_tail.replace('-', ' '))

    return titles


async def _get_cinemeta_target_year(
    content_type: str,
    stremio_id: str,
    season_number: int | None,
    episode_number: int,
) -> int | None:
    if content_type == 'movie' or season_number is None:
        return None

    meta = await _fetch_cinemeta_meta(content_type, stremio_id)
    if meta is None:
        return None
    return _extract_cinemeta_target_year(meta, season_number, episode_number)


def _extract_cinemeta_target_year(meta: dict, season_number: int | None, episode_number: int) -> int | None:
    if season_number is None:
        return None

    videos = meta.get('videos')
    if not isinstance(videos, list):
        return None

    fallback_year: int | None = None
    for item in videos:
        if not isinstance(item, dict):
            continue

        item_season = item.get('season')
        item_episode = item.get('number', item.get('episode'))
        if item_season != season_number:
            continue

        item_year = _extract_year_from_cinemeta_video(item)
        if fallback_year is None:
            fallback_year = item_year

        if item_episode == episode_number:
            return item_year

    return fallback_year


def _extract_year_from_cinemeta_video(video: dict) -> int | None:
    for key in ('firstAired', 'released'):
        value = video.get(key)
        if not isinstance(value, str) or len(value) < 4:
            continue
        year_text = value[:4]
        if year_text.isdigit():
            return int(year_text)
    return None


def _merge_search_titles(base_titles: list[str], enriched_titles: list[str], season_number: int | None) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()

    ordered_enriched = list(enriched_titles)
    if season_number is not None:
        ordered_enriched = _prioritize_titles_for_requested_season(base_titles, ordered_enriched, season_number)
        for value in ordered_enriched + base_titles:
            _append_unique_title(merged, seen, value)
        return merged

    for value in base_titles + ordered_enriched:
        _append_unique_title(merged, seen, value)
    return merged


def _prioritize_titles_for_requested_season(
    base_titles: list[str],
    enriched_titles: list[str],
    season_number: int,
) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in enriched_titles:
        _append_unique_title(deduped, seen, value)

    if season_number <= 0:
        return deduped

    normalized_roots = {
        _normalize_search_text(value)
        for value in base_titles
        if isinstance(value, str) and value.strip()
    }
    season_variants = [value for value in deduped if _looks_like_season_variant(value, normalized_roots)]
    if not season_variants or season_number > len(season_variants):
        return deduped

    preferred = season_variants[season_number - 1]
    return [preferred] + [value for value in deduped if value != preferred]


def _looks_like_season_variant(title: str, normalized_roots: set[str]) -> bool:
    normalized_title = _normalize_search_text(title)
    generic_suffixes = {
        'series',
        'the series',
        'serial',
        'serie',
        'serien',
    }

    for root in normalized_roots:
        if not root or normalized_title == root or not normalized_title.startswith(root):
            continue

        suffix = normalized_title[len(root):].strip(' :-')
        if not suffix or suffix in generic_suffixes:
            return False
        return True

    return False


async def _get_anilist_search_titles(
    content_type: str,
    seed_titles: list[str],
    target_year: int | None = None,
) -> list[str]:
    if not seed_titles:
        return []

    titles: list[str] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for seed_title in seed_titles[:4]:
            if target_year is None:
                cached_titles = await _get_cached_anilist_titles(content_type, seed_title)
                if cached_titles is not None:
                    for value in cached_titles:
                        _append_unique_title(titles, seen, value)
                    continue

            response = await client.post(
                'https://graphql.anilist.co',
                json={'query': ANILIST_SEARCH_QUERY, 'variables': {'search': seed_title}},
                headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
            )
            if response.status_code != 200:
                continue

            media_items = response.json().get('data', {}).get('Page', {}).get('media', [])
            best_match = _select_best_anilist_match(seed_title, media_items, content_type, target_year=target_year)
            if best_match is None:
                continue

            cached_payload: list[str] = []
            media_title = best_match.get('title') or {}
            for value in (
                media_title.get('english'),
                media_title.get('romaji'),
                media_title.get('native'),
            ):
                if isinstance(value, str):
                    cached_payload.append(value)
                _append_unique_title(titles, seen, value)

            synonyms = best_match.get('synonyms') or []
            if isinstance(synonyms, list):
                for value in synonyms:
                    if isinstance(value, str):
                        cached_payload.append(value)
                    _append_unique_title(titles, seen, value)

            if target_year is None:
                await _save_cached_anilist_titles(content_type, seed_title, cached_payload)

    return titles


async def _get_cached_cinemeta_titles(content_type: str, stremio_id: str) -> list[str] | None:
    async with get_session() as session:
        statement = select(CinemetaTitleCache).where(
            CinemetaTitleCache.content_type == content_type,
            CinemetaTitleCache.stremio_id == stremio_id,
        )
        cached_entry = await session.scalar(statement)
        if cached_entry is None or not _is_cache_fresh(cached_entry.updated_at, cached_entry.created_at, CINEMETA_CACHE_TTL):
            return None

        try:
            payload = json.loads(cached_entry.titles_json)
        except json.JSONDecodeError:
            logger.warning(
                'Failed to decode cached Cinemeta titles: content_type=%s stremio_id=%s',
                content_type,
                stremio_id,
                exc_info=True,
            )
            return None

        if not isinstance(payload, list):
            return None

        return [value for value in payload if isinstance(value, str) and value.strip()]


async def _save_cached_cinemeta_titles(content_type: str, stremio_id: str, titles: list[str]) -> None:
    payload = [value for value in titles if isinstance(value, str) and value.strip()]
    if not payload:
        return

    async with get_session() as session:
        statement = select(CinemetaTitleCache).where(
            CinemetaTitleCache.content_type == content_type,
            CinemetaTitleCache.stremio_id == stremio_id,
        )
        cached_entry = await session.scalar(statement)

        if cached_entry is None:
            session.add(
                CinemetaTitleCache(
                    content_type=content_type,
                    stremio_id=stremio_id,
                    titles_json=json.dumps(payload, ensure_ascii=False),
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                )
            )
        else:
            cached_entry.titles_json = json.dumps(payload, ensure_ascii=False)
            cached_entry.updated_at = _utcnow()

        await session.commit()


async def _get_cached_anilist_titles(content_type: str, seed_title: str) -> list[str] | None:
    cache_key = _normalize_search_text(seed_title)
    if not cache_key:
        return None

    async with get_session() as session:
        statement = select(AniListTitleCache).where(
            AniListTitleCache.content_type == content_type,
            AniListTitleCache.seed_title == cache_key,
        )
        cached_entry = await session.scalar(statement)
        if cached_entry is None or not _is_cache_fresh(cached_entry.updated_at, cached_entry.created_at, ANILIST_CACHE_TTL):
            return None

        try:
            payload = json.loads(cached_entry.titles_json)
        except json.JSONDecodeError:
            logger.warning(
                'Failed to decode cached AniList titles: content_type=%s seed_title=%s cache_key=%s',
                content_type,
                seed_title,
                cache_key,
                exc_info=True,
            )
            return None

        if not isinstance(payload, list):
            return None

        return [value for value in payload if isinstance(value, str) and value.strip()]


async def _save_cached_anilist_titles(content_type: str, seed_title: str, titles: list[str]) -> None:
    cache_key = _normalize_search_text(seed_title)
    if not cache_key:
        return

    payload = [value for value in titles if isinstance(value, str) and value.strip()]
    if not payload:
        return

    async with get_session() as session:
        statement = select(AniListTitleCache).where(
            AniListTitleCache.content_type == content_type,
            AniListTitleCache.seed_title == cache_key,
        )
        cached_entry = await session.scalar(statement)

        if cached_entry is None:
            session.add(
                AniListTitleCache(
                    content_type=content_type,
                    seed_title=cache_key,
                    titles_json=json.dumps(payload, ensure_ascii=False),
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                )
            )
        else:
            cached_entry.titles_json = json.dumps(payload, ensure_ascii=False)
            cached_entry.updated_at = _utcnow()

        await session.commit()


def _select_best_anilist_match(
    seed_title: str,
    media_items: list[dict],
    content_type: str,
    target_year: int | None = None,
) -> dict | None:
    if not media_items:
        return None

    return max(media_items, key=lambda item: _score_anilist_match(seed_title, item, content_type, target_year=target_year))


def _score_anilist_match(
    seed_title: str,
    media_item: dict,
    content_type: str,
    target_year: int | None = None,
) -> int:
    score = 0
    normalized_seed = _normalize_search_text(seed_title)
    candidates = _extract_anilist_titles(media_item)
    normalized_candidates = [_normalize_search_text(value) for value in candidates if value]

    if normalized_seed in normalized_candidates:
        score += 120
    elif any(normalized_seed and normalized_seed in value for value in normalized_candidates):
        score += 45
    elif any(value and value in normalized_seed for value in normalized_candidates):
        score += 35

    media_format = (media_item.get('format') or '').upper()
    if content_type == 'movie':
        if media_format == 'MOVIE':
            score += 80
        elif media_format in {'SPECIAL', 'OVA', 'ONA'}:
            score += 20
        elif media_format == 'TV':
            score -= 25
    else:
        if media_format == 'TV':
            score += 80
        elif media_format in {'ONA', 'OVA', 'TV_SHORT'}:
            score += 35
        elif media_format == 'MOVIE':
            score -= 25

    english_title = ((media_item.get('title') or {}).get('english') or '')
    if english_title and _normalize_search_text(english_title) == normalized_seed:
        score += 25

    if target_year is not None:
        media_year = _extract_anilist_year(media_item)
        if media_year is None:
            score -= 10
        else:
            year_delta = abs(media_year - target_year)
            if year_delta == 0:
                score += 140
            elif year_delta == 1:
                score += 95
            elif year_delta <= 3:
                score += 55
            elif year_delta <= 6:
                score += 20
            else:
                score -= min(year_delta * 5, 80)

    return score


def _extract_anilist_year(media_item: dict) -> int | None:
    season_year = media_item.get('seasonYear')
    if isinstance(season_year, int) and season_year > 0:
        return season_year

    start_date = media_item.get('startDate') or {}
    start_year = start_date.get('year') if isinstance(start_date, dict) else None
    if isinstance(start_year, int) and start_year > 0:
        return start_year

    return None


def _extract_anilist_titles(media_item: dict) -> list[str]:
    values: list[str] = []
    media_title = media_item.get('title') or {}
    for value in (
        media_title.get('english'),
        media_title.get('romaji'),
        media_title.get('native'),
    ):
        if isinstance(value, str):
            values.append(value)

    synonyms = media_item.get('synonyms') or []
    if isinstance(synonyms, list):
        for value in synonyms:
            if isinstance(value, str):
                values.append(value)

    return values


def _append_unique_title(titles: list[str], seen: set[str], value: str | None) -> None:
    if not value:
        return

    normalized = value.strip()
    if not normalized:
        return

    key = normalized.casefold()
    if key in seen:
        return

    seen.add(key)
    titles.append(normalized)


def _append_title_variants(titles: list[str], seen: set[str], value: str | None) -> None:
    _append_unique_title(titles, seen, value)
    if not value:
        return

    normalized = _normalize_search_text(value)
    if normalized != value.strip():
        _append_unique_title(titles, seen, normalized)


def _normalize_search_text(value: str) -> str:
    normalized = value.casefold().strip()
    normalized = re.sub(r'[._:]+', ' ', normalized)
    normalized = re.sub(r"[\"'`!?(),\[\]{}]+", ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized.strip()


def _is_cache_fresh(updated_at: datetime | None, created_at: datetime | None, ttl: timedelta) -> bool:
    cached_at = updated_at or created_at
    if cached_at is None:
        return False

    now = _utcnow()
    if cached_at.tzinfo is None:
        now = now.replace(tzinfo=None)

    return now - cached_at <= ttl


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)