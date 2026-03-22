from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re

import httpx
from sqlalchemy import select

from db.models import AniListTitleCache, CinemetaTitleCache, KitsuMappingsCache
from db.session import get_session
from parsers.kitsu import get_anime, get_anime_search_titles
from settings import ANILIST_CACHE_TTL_HOURS, CINEMETA_CACHE_TTL_HOURS, KITSU_MAPPINGS_CACHE_TTL_HOURS


ANILIST_SEARCH_QUERY = '''
query ($search: String!) {
    Page(page: 1, perPage: 8) {
        media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
            format
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
_SEARCH_TARGETS_CACHE: dict[tuple[str, str, str], tuple[datetime, SearchTargets]] = {}


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
    cache_key = (content_type, stream_request.provider, stream_request.catalog_id)
    cached_targets = _get_cached_search_targets(cache_key)
    if cached_targets is not None:
        return cached_targets

    if stream_request.provider == 'kitsu':
        anime, external_urls = await _get_kitsu_search_data(int(stream_request.catalog_id))
        titles = get_anime_search_titles(anime)
        targets = SearchTargets(titles=titles, external_urls=external_urls)
        _save_cached_search_targets(cache_key, targets)
        return targets

    if stream_request.provider == 'anilist':
        titles, external_urls = await get_anilist_search_targets(int(stream_request.catalog_id))
        targets = SearchTargets(titles=titles, external_urls=external_urls)
        _save_cached_search_targets(cache_key, targets)
        return targets

    if stream_request.provider == 'tt':
        targets = SearchTargets(
            titles=await get_cinemeta_search_titles(content_type, stream_request.catalog_id),
            external_urls=[],
        )
        _save_cached_search_targets(cache_key, targets)
        return targets

    raise ValueError(f'Unsupported provider: {stream_request.provider}')


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


def _get_cached_search_targets(cache_key: tuple[str, str, str]) -> SearchTargets | None:
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


def _save_cached_search_targets(cache_key: tuple[str, str, str], search_targets: SearchTargets) -> None:
    _SEARCH_TARGETS_CACHE[cache_key] = (
        _utcnow(),
        SearchTargets(
            titles=list(search_targets.titles),
            external_urls=list(search_targets.external_urls),
        ),
    )


async def get_cinemeta_search_titles(content_type: str, stremio_id: str) -> list[str]:
    cached_titles = await _get_cached_cinemeta_titles(content_type, stremio_id)
    if cached_titles is not None:
        titles = list(cached_titles)
        seen = {value.casefold() for value in titles}

        def add(value: str | None) -> None:
            _append_title_variants(titles, seen, value)

        enriched_titles = await _get_anilist_search_titles(content_type, titles)
        for value in enriched_titles:
            add(value)
        return titles

    url = f'https://v3-cinemeta.strem.io/meta/{content_type}/{stremio_id}.json'
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(url)

    if response.status_code != 200:
        return []

    meta = response.json().get('meta') or {}
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

    await _save_cached_cinemeta_titles(content_type, stremio_id, titles)

    enriched_titles = await _get_anilist_search_titles(content_type, titles)
    for value in enriched_titles:
        add(value)

    return titles


async def _get_anilist_search_titles(content_type: str, seed_titles: list[str]) -> list[str]:
    if not seed_titles:
        return []

    titles: list[str] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for seed_title in seed_titles[:4]:
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
            best_match = _select_best_anilist_match(seed_title, media_items, content_type)
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


def _select_best_anilist_match(seed_title: str, media_items: list[dict], content_type: str) -> dict | None:
    if not media_items:
        return None

    return max(media_items, key=lambda item: _score_anilist_match(seed_title, item, content_type))


def _score_anilist_match(seed_title: str, media_item: dict, content_type: str) -> int:
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

    return score


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