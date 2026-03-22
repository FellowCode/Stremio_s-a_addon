import asyncio
import kitsu_extended



async def get_anime(kitsu_id: int) -> kitsu_extended.Anime:
    # Инициализация клиента
    client = kitsu_extended.Client()
    anime = await client.get_anime(kitsu_id)
    await client.close()
    return anime


def get_anime_search_titles(anime: kitsu_extended.Anime) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        if not value:
            return
        normalized = value.strip()
        if not normalized:
            return
        key = normalized.casefold()
        if key in seen:
            return
        seen.add(key)
        candidates.append(normalized)

    add(getattr(anime, 'canonical_title', None))

    titles = getattr(anime, 'titles', None)
    if isinstance(titles, dict):
        for value in titles.values():
            add(value)

    abbreviated_titles = getattr(anime, 'abbreviated_titles', None)
    if isinstance(abbreviated_titles, list):
        for value in abbreviated_titles:
            add(value)

    slug = getattr(anime, 'slug', None)
    if isinstance(slug, str):
        add(slug.replace('-', ' '))

    return candidates