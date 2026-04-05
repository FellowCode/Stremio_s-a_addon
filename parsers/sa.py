import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import httpx
import json
import logging
import os
import re
import urllib.parse
from bs4 import BeautifulSoup
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload
from typing import Literal

from db.models import AnimeReleaseStatusCache, SmotretAnimeEpisodeCache, SmotretAnimeTranslationCache, SmotretAnimeVideoCache
from db.session import get_session
from settings import (
    ANIME_STATUS_CACHE_TTL_HOURS,
    FLARESOLVERR_HOST,
    FLARESOLVERR_HTTP_TIMEOUT_SECONDS,
    FLARESOLVERR_MAX_TIMEOUT_MS,
    FLARESOLVERR_PORT,
    FLARESOLVERR_PERSIST_SESSIONS,
    FLARESOLVERR_SESSION_STATE_FILE,
    FLARESOLVERR_SESSION_POOL_SIZE,
    FINISHED_EPISODE_CACHE_TTL_HOURS,
    FINISHED_STABLE_AFTER_DAYS,
    FINISHED_TRANSLATION_CACHE_TTL_HOURS,
    LOGIN,
    ONGOING_EPISODE_CACHE_TTL_HOURS,
    ONGOING_TRANSLATION_CACHE_TTL_HOURS,
    PASSWORD,
    PARSING_PROXY_PROVIDER,
    QUALITY_PROFILES,
    SA_HOST,
    SCRAPE_TOKEN,
    VIDEO_CACHE_TTL_HOURS,
    VIDEO_PROBE_TIMEOUT_SECONDS,
)


logger = logging.getLogger(__name__)


def _response_text_preview(response: httpx.Response, max_length: int = 600) -> str:
    body = response.text.strip()
    if not body:
        return '<empty>'

    normalized = re.sub(r'\s+', ' ', body)
    if len(normalized) <= max_length:
        return normalized
    return normalized[:max_length] + '...'


def _raise_for_status_with_context(response: httpx.Response, context: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f'{context}: status={response.status_code} url={response.request.url} body={_response_text_preview(response)}'
        ) from exc


class EpisodeSchema(BaseModel):
    url: str
    number: int


class TranslationSchema(BaseModel):
    type: Literal['sub', 'dub']
    name: str
    url: str


class VideoSchema(BaseModel):
    translation_url: str
    quality_profile: str
    video_url: str


@dataclass(slots=True)
class ProxyResponse:
    text: str
    status_code: int
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class ProxyRequestKey:
    provider: str
    command: str
    url: str
    post_data: str | None
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ParsingRequestKey:
    episode_number: int
    titles: tuple[str, ...]
    external_search_urls: tuple[str, ...]


class SmotretAnimeProxyClient:
    client: httpx.AsyncClient | None = None
    cookies: str | None = None
    flaresolverr_session_ids: list[str] = []
    flaresolverr_session_queue: asyncio.Queue[str] | None = None
    init_lock: asyncio.Lock | None = None
    inflight_lock: asyncio.Lock | None = None
    auth_refresh_lock: asyncio.Lock | None = None
    inflight_requests: dict[ProxyRequestKey, asyncio.Task[ProxyResponse]] = {}

    @classmethod
    def _should_persist_flaresolverr_sessions(cls) -> bool:
        return cls._get_proxy_provider() == 'flaresolverr' and FLARESOLVERR_PERSIST_SESSIONS

    @classmethod
    def _load_persisted_flaresolverr_session_ids(cls) -> list[str]:
        if not cls._should_persist_flaresolverr_sessions() or not os.path.exists(FLARESOLVERR_SESSION_STATE_FILE):
            return []

        try:
            with open(FLARESOLVERR_SESSION_STATE_FILE, 'r', encoding='utf-8') as file_handle:
                payload = json.load(file_handle)
        except (OSError, json.JSONDecodeError):
            logger.warning('Failed to load persisted FlareSolverr sessions from %s', FLARESOLVERR_SESSION_STATE_FILE, exc_info=True)
            return []

        if not isinstance(payload, dict):
            return []
        if payload.get('host') != FLARESOLVERR_HOST or payload.get('port') != FLARESOLVERR_PORT:
            logger.info('Ignoring persisted FlareSolverr sessions because host or port changed')
            return []

        session_ids = payload.get('session_ids') or []
        if not isinstance(session_ids, list):
            return []

        normalized: list[str] = []
        seen: set[str] = set()
        for value in session_ids:
            if not isinstance(value, str):
                continue
            session_id = value.strip()
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            normalized.append(session_id)
        return normalized

    @classmethod
    def _save_persisted_flaresolverr_session_ids(cls, session_ids: Sequence[str]) -> None:
        if not cls._should_persist_flaresolverr_sessions():
            return

        payload = {
            'host': FLARESOLVERR_HOST,
            'port': FLARESOLVERR_PORT,
            'session_ids': [session_id for session_id in session_ids if isinstance(session_id, str) and session_id],
        }
        try:
            with open(FLARESOLVERR_SESSION_STATE_FILE, 'w', encoding='utf-8') as file_handle:
                json.dump(payload, file_handle, ensure_ascii=True, indent=2)
        except OSError:
            logger.warning('Failed to persist FlareSolverr sessions to %s', FLARESOLVERR_SESSION_STATE_FILE, exc_info=True)

    @classmethod
    def _clear_persisted_flaresolverr_session_ids(cls) -> None:
        if not os.path.exists(FLARESOLVERR_SESSION_STATE_FILE):
            return

        try:
            os.remove(FLARESOLVERR_SESSION_STATE_FILE)
        except OSError:
            logger.warning('Failed to remove persisted FlareSolverr sessions file %s', FLARESOLVERR_SESSION_STATE_FILE, exc_info=True)

    @classmethod
    def _get_proxy_provider(cls) -> str:
        provider = PARSING_PROXY_PROVIDER.casefold().strip()
        if provider not in {'scrapedo', 'flaresolverr'}:
            raise ValueError(f'Unsupported parsing proxy provider: {PARSING_PROXY_PROVIDER}')
        return provider

    @classmethod
    def _normalize_url(cls, url: str) -> str:
        if url.startswith('/'):
            return f'https://{SA_HOST}{url}'
        if not url.startswith(('https://', 'http://')):
            return f'https://{url}'
        return url

    @classmethod
    def _build_scrape_url(cls, url: str) -> str:
        normalized_url = cls._normalize_url(url)
        scrape_url = f'https://api.scrape.do/?token={SCRAPE_TOKEN}&url={normalized_url}&sessionId=900'
        if cls.cookies is not None:
            scrape_url += f'&setCookies={urllib.parse.quote(cls.cookies)}'
        return scrape_url

    @classmethod
    def _get_flaresolverr_url(cls) -> str:
        host = FLARESOLVERR_HOST.strip()
        if not host:
            raise ValueError('FLARESOLVERR_HOST must not be empty')
        if not host.startswith(('http://', 'https://')):
            host = f'http://{host}'
        return f'{host.rstrip("/")}:{FLARESOLVERR_PORT}/v1'

    @classmethod
    def _get_init_lock(cls) -> asyncio.Lock:
        if cls.init_lock is None:
            cls.init_lock = asyncio.Lock()
        return cls.init_lock

    @classmethod
    def _get_inflight_lock(cls) -> asyncio.Lock:
        if cls.inflight_lock is None:
            cls.inflight_lock = asyncio.Lock()
        return cls.inflight_lock

    @classmethod
    def _get_auth_refresh_lock(cls) -> asyncio.Lock:
        if cls.auth_refresh_lock is None:
            cls.auth_refresh_lock = asyncio.Lock()
        return cls.auth_refresh_lock

    @classmethod
    def _get_flaresolverr_session_queue(cls) -> asyncio.Queue[str]:
        if cls.flaresolverr_session_queue is None:
            cls.flaresolverr_session_queue = asyncio.Queue()
        return cls.flaresolverr_session_queue

    @classmethod
    def _get_flaresolverr_session_pool_size(cls) -> int:
        return max(1, int(FLARESOLVERR_SESSION_POOL_SIZE))

    @classmethod
    def _clone_response(cls, response: ProxyResponse) -> ProxyResponse:
        return ProxyResponse(text=response.text, status_code=response.status_code, headers=dict(response.headers))

    @classmethod
    def _response_requires_auth(cls, response: ProxyResponse, normalized_url: str) -> bool:
        login_url = cls._normalize_url('/users/login')
        if normalized_url == login_url:
            return False
        if response.status_code != 200:
            return False

        html = response.text
        return (
            'LoginForm[username]' in html
            and 'LoginForm[password]' in html
            and 'input name="csrf"' in html
        )

    @classmethod
    def _build_request_key(
        cls,
        command: str,
        url: str,
        *,
        post_data: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ProxyRequestKey:
        return ProxyRequestKey(
            provider=cls._get_proxy_provider(),
            command=command,
            url=cls._normalize_url(url),
            post_data=post_data,
            headers=tuple(sorted((headers or {}).items())),
        )

    @classmethod
    def _register_inflight_cleanup(cls, request_key: ProxyRequestKey, task: asyncio.Task[ProxyResponse]) -> None:
        def cleanup(_: asyncio.Task[ProxyResponse]) -> None:
            current_task = cls.inflight_requests.get(request_key)
            if current_task is task:
                cls.inflight_requests.pop(request_key, None)

        task.add_done_callback(cleanup)

    @classmethod
    async def get(cls, url: str) -> ProxyResponse:
        return await cls._request('request.get', url)

    @classmethod
    async def post(cls, url: str, data: dict[str, str]) -> ProxyResponse:
        return await cls._request(
            'request.post',
            url,
            form_data=data,
            post_data=urllib.parse.urlencode(data),
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )

    @classmethod
    async def _request(
        cls,
        command: str,
        url: str,
        *,
        form_data: dict[str, str] | None = None,
        post_data: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ProxyResponse:
        if cls.client is None:
            await cls.init()

        request_key = cls._build_request_key(command, url, post_data=post_data, headers=headers)
        async with cls._get_inflight_lock():
            task = cls.inflight_requests.get(request_key)
            if task is None:
                task = asyncio.create_task(
                    cls._execute_request(
                        command,
                        request_key.url,
                        form_data=form_data,
                        post_data=post_data,
                        headers=headers,
                    )
                )
                cls.inflight_requests[request_key] = task
                cls._register_inflight_cleanup(request_key, task)

        try:
            return cls._clone_response(await asyncio.shield(task))
        except Exception:
            logger.exception('Proxy request failed: provider=%s command=%s url=%s', request_key.provider, request_key.command, request_key.url)
            raise

    @classmethod
    async def _execute_request(
        cls,
        command: str,
        normalized_url: str,
        *,
        form_data: dict[str, str] | None = None,
        post_data: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ProxyResponse:
        provider = cls._get_proxy_provider()
        if provider == 'scrapedo':
            return await cls._execute_scrapedo_request(command, normalized_url, form_data=form_data)

        session_id = await cls._get_flaresolverr_session_queue().get()
        try:
            return await cls._execute_flaresolverr_request(
                session_id,
                command,
                normalized_url,
                post_data=post_data,
                headers=headers,
            )
        finally:
            cls._get_flaresolverr_session_queue().put_nowait(session_id)

    @classmethod
    async def _execute_scrapedo_request(
        cls,
        command: str,
        normalized_url: str,
        *,
        form_data: dict[str, str] | None = None,
    ) -> ProxyResponse:
        response = await cls._perform_scrapedo_request(command, normalized_url, form_data=form_data)
        if not cls._response_requires_auth(response, normalized_url):
            return response

        logger.warning('SmotretAnime scrapedo authorization expired, refreshing auth and retrying request: url=%s', normalized_url)
        await cls._refresh_scrapedo_auth(force=True)
        return await cls._perform_scrapedo_request(command, normalized_url, form_data=form_data)

    @classmethod
    async def _perform_scrapedo_request(
        cls,
        command: str,
        normalized_url: str,
        *,
        form_data: dict[str, str] | None = None,
    ) -> ProxyResponse:
        if command == 'request.get':
            response = await cls.client.get(cls._build_scrape_url(normalized_url))
        else:
            response = await cls.client.post(cls._build_scrape_url(normalized_url), data=form_data)
        return ProxyResponse(text=response.text, status_code=response.status_code, headers=dict(response.headers))

    @classmethod
    async def _execute_flaresolverr_request(
        cls,
        session_id: str,
        command: str,
        normalized_url: str,
        *,
        post_data: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ProxyResponse:
        response = await cls._flaresolverr_request_with_session(
            session_id,
            command,
            normalized_url,
            post_data=post_data,
            headers=headers,
        )
        if not cls._response_requires_auth(response, normalized_url):
            return response

        logger.warning(
            'SmotretAnime flaresolverr authorization expired, refreshing session auth and retrying request: session_id=%s url=%s',
            session_id,
            normalized_url,
        )
        await cls._refresh_flaresolverr_session_auth(session_id)
        return await cls._flaresolverr_request_with_session(
            session_id,
            command,
            normalized_url,
            post_data=post_data,
            headers=headers,
        )

    @classmethod
    async def _flaresolverr_request_with_session(
        cls,
        session_id: str,
        command: str,
        url: str,
        *,
        post_data: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ProxyResponse:
        payload: dict[str, object] = {
            'cmd': command,
            'url': cls._normalize_url(url),
            'maxTimeout': FLARESOLVERR_MAX_TIMEOUT_MS,
            'session': session_id,
        }
        if post_data is not None:
            payload['postData'] = post_data
        if headers is not None:
            payload['headers'] = headers

        response = await cls.client.post(cls._get_flaresolverr_url(), json=payload)
        _raise_for_status_with_context(response, 'FlareSolverr request failed')

        body = response.json()
        if body.get('status') != 'ok':
            message = body.get('message') or 'Unknown FlareSolverr error'
            raise RuntimeError(f'FlareSolverr request failed: {message}')

        solution = body.get('solution') or {}
        solution_headers = solution.get('headers') or {}
        normalized_headers = {str(key): str(value) for key, value in solution_headers.items()}
        return ProxyResponse(
            text=solution.get('response') or '',
            status_code=int(solution.get('status') or 0),
            headers=normalized_headers,
        )

    @classmethod
    def _extract_login_csrf(cls, html: str) -> str:
        soup = BeautifulSoup(html, 'html.parser')
        csrf_input = soup.select_one('input[name=csrf]')
        if csrf_input is None:
            raise RuntimeError('Unable to find SmotretAnime login CSRF token')
        csrf = csrf_input.get('value')
        if not isinstance(csrf, str) or not csrf:
            raise RuntimeError('SmotretAnime login CSRF token is empty')
        return csrf

    @classmethod
    async def _login_flaresolverr_session(cls, session_id: str) -> None:
        logger.info('Starting SmotretAnime login in FlareSolverr session: session_id=%s', session_id)
        login_page = await cls._flaresolverr_request_with_session(session_id, 'request.get', '/users/login')
        csrf = cls._extract_login_csrf(login_page.text)
        data = {
            'csrf': csrf,
            'LoginForm[username]': LOGIN,
            'LoginForm[password]': PASSWORD,
            'yt0': '',
            'dynpage': '0',
        }
        login_response = await cls._flaresolverr_request_with_session(
            session_id,
            'request.post',
            '/users/login',
            post_data=urllib.parse.urlencode(data),
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        if login_response.status_code != 200:
            raise RuntimeError(f'FlareSolverr login failed with status {login_response.status_code}')
        logger.info('SmotretAnime login completed in FlareSolverr session: session_id=%s', session_id)

    @classmethod
    async def _is_flaresolverr_session_authenticated(cls, session_id: str) -> bool:
        logger.info('Checking persisted SmotretAnime auth in FlareSolverr session: session_id=%s', session_id)
        response = await cls._flaresolverr_request_with_session(session_id, 'request.get', '/')
        authenticated = not cls._response_requires_auth(response, cls._normalize_url('/'))
        if authenticated:
            logger.info('Persisted FlareSolverr session is still authenticated: session_id=%s', session_id)
        else:
            logger.warning('Persisted FlareSolverr session requires reauthentication: session_id=%s', session_id)
        return authenticated

    @classmethod
    async def _refresh_flaresolverr_session_auth(cls, session_id: str) -> None:
        async with cls._get_auth_refresh_lock():
            logger.info('Refreshing SmotretAnime auth in FlareSolverr session: session_id=%s', session_id)
            await cls._login_flaresolverr_session(session_id)

    @classmethod
    async def _refresh_scrapedo_auth(cls, *, force: bool = False) -> None:
        async with cls._get_auth_refresh_lock():
            if not force and cls.cookies is not None:
                logger.info('Skipping SmotretAnime scrapedo login because cookies are already loaded')
                return

            logger.info('Starting SmotretAnime login via scrapedo')
            login_page_response = await cls._perform_scrapedo_request('request.get', cls._normalize_url('/users/login'))
            cookies = login_page_response.headers.get('scrape.do-cookies')
            if not cookies:
                raise RuntimeError('Scrape.do login page did not return cookies')

            cls.cookies = cookies
            csrf = cls._extract_login_csrf(login_page_response.text)
            login_response = await cls._perform_scrapedo_request(
                'request.post',
                cls._normalize_url('/users/login'),
                form_data={
                    'csrf': csrf,
                    'LoginForm[username]': LOGIN,
                    'LoginForm[password]': PASSWORD,
                    'yt0': '',
                    'dynpage': '0',
                },
            )
            if login_response.status_code != 200:
                raise RuntimeError(f'Scrape.do login failed with status {login_response.status_code}')

            refreshed_cookies = login_response.headers.get('scrape.do-cookies')
            if not refreshed_cookies:
                raise RuntimeError('Scrape.do login response did not return cookies')

            cls.cookies = refreshed_cookies
            with open('cookies.txt', 'w') as file_handle:
                file_handle.write(cls.cookies)
            logger.info('SmotretAnime login via scrapedo completed and cookies were persisted')

    @classmethod
    async def _create_flaresolverr_session(cls, session_id: str | None = None) -> tuple[str, bool]:
        if session_id:
            logger.info('Opening persisted FlareSolverr session for SmotretAnime authorization: session_id=%s', session_id)
        else:
            logger.info('Creating FlareSolverr session for SmotretAnime authorization')

        payload: dict[str, object] = {'cmd': 'sessions.create'}
        if session_id:
            payload['session'] = session_id

        response = await cls.client.post(cls._get_flaresolverr_url(), json=payload)
        _raise_for_status_with_context(response, 'FlareSolverr session create failed')

        body = response.json()
        if body.get('status') != 'ok':
            message = body.get('message') or 'Unknown FlareSolverr error'
            raise RuntimeError(f'FlareSolverr session create failed: {message}')

        resolved_session_id = body.get('session')
        if not isinstance(resolved_session_id, str) or not resolved_session_id:
            raise RuntimeError('FlareSolverr did not return a valid session id')

        reused_existing = body.get('message') == 'Session already exists.'
        if reused_existing:
            logger.info('Reused existing FlareSolverr session for SmotretAnime authorization: session_id=%s', resolved_session_id)
        else:
            logger.info('Created FlareSolverr session for SmotretAnime authorization: session_id=%s', resolved_session_id)
        return resolved_session_id, reused_existing

    @classmethod
    async def _destroy_specific_flaresolverr_sessions(cls, session_ids: Sequence[str]) -> None:
        if cls.client is None:
            return

        for session_id in session_ids:
            try:
                await cls.client.post(
                    cls._get_flaresolverr_url(),
                    json={'cmd': 'sessions.destroy', 'session': session_id},
                )
            except httpx.HTTPError:
                logger.warning('Failed to destroy FlareSolverr session: session_id=%s', session_id, exc_info=True)

    @classmethod
    async def _destroy_flaresolverr_sessions(cls) -> None:
        if cls.client is None or not cls.flaresolverr_session_ids:
            return
        if cls._should_persist_flaresolverr_sessions():
            logger.info('Leaving FlareSolverr sessions running for reuse across addon restarts: sessions=%s', len(cls.flaresolverr_session_ids))
            cls._save_persisted_flaresolverr_session_ids(cls.flaresolverr_session_ids)
            cls.flaresolverr_session_queue = None
            return
        try:
            await cls._destroy_specific_flaresolverr_sessions(cls.flaresolverr_session_ids)
        except httpx.HTTPError:
            logger.warning('Failed to destroy one or more FlareSolverr sessions during shutdown', exc_info=True)
        finally:
            cls._clear_persisted_flaresolverr_session_ids()
            cls.flaresolverr_session_ids = []
            cls.flaresolverr_session_queue = None

    @classmethod
    async def init(cls) -> dict[str, object]:
        async with cls._get_init_lock():
            if cls.client is not None:
                return {'success': True}

            provider = cls._get_proxy_provider()
            logger.info('Initializing SmotretAnime proxy client: provider=%s', provider)
            timeout = FLARESOLVERR_HTTP_TIMEOUT_SECONDS if provider == 'flaresolverr' else 15.0
            cls.client = httpx.AsyncClient(timeout=timeout)

            if provider == 'scrapedo' and os.path.exists('cookies.txt'):
                with open('cookies.txt', 'r') as file_handle:
                    cls.cookies = file_handle.read().strip()
                logger.info('Loaded persisted SmotretAnime scrapedo cookies from cookies.txt')
                return {'success': True}

            if provider == 'scrapedo' and not SCRAPE_TOKEN:
                raise ValueError('SCRAPE_TOKEN is required when PARSING_PROXY_PROVIDER is scrapedo')

            if provider == 'flaresolverr':
                session_queue = cls._get_flaresolverr_session_queue()
                cls.flaresolverr_session_ids = []
                persisted_session_ids = cls._load_persisted_flaresolverr_session_ids()
                desired_pool_size = cls._get_flaresolverr_session_pool_size()
                logger.info(
                    'Preparing FlareSolverr session pool for SmotretAnime: pool_size=%s',
                    desired_pool_size,
                )
                if persisted_session_ids:
                    logger.info('Found persisted FlareSolverr sessions for reuse: count=%s', len(persisted_session_ids))

                active_session_ids = persisted_session_ids[:desired_pool_size]
                stale_session_ids = persisted_session_ids[desired_pool_size:]
                if stale_session_ids:
                    logger.info('Destroying stale persisted FlareSolverr sessions beyond pool size: count=%s', len(stale_session_ids))
                    await cls._destroy_specific_flaresolverr_sessions(stale_session_ids)

                while len(active_session_ids) < desired_pool_size:
                    active_session_ids.append('')

                for persisted_session_id in active_session_ids:
                    session_id, reused_existing = await cls._create_flaresolverr_session(persisted_session_id or None)
                    if not reused_existing or not await cls._is_flaresolverr_session_authenticated(session_id):
                        await cls._login_flaresolverr_session(session_id)
                    cls.flaresolverr_session_ids.append(session_id)
                    session_queue.put_nowait(session_id)
                cls._save_persisted_flaresolverr_session_ids(cls.flaresolverr_session_ids)
                logger.info('SmotretAnime proxy client initialized with FlareSolverr sessions=%s', len(cls.flaresolverr_session_ids))
                return {'success': True}

            await cls._refresh_scrapedo_auth(force=True)
            logger.info('SmotretAnime proxy client initialized with scrapedo authorization')
            return {'success': True}

    @classmethod
    async def close(cls) -> None:
        async with cls._get_inflight_lock():
            inflight_tasks = list(cls.inflight_requests.values())
            cls.inflight_requests = {}

        for task in inflight_tasks:
            task.cancel()

        await cls._destroy_flaresolverr_sessions()
        if cls.client is not None:
            await cls.client.aclose()
            cls.client = None
        cls.cookies = None



class SmotretAnimeParser:
    quality_max_height = QUALITY_PROFILES
    client: httpx.AsyncClient | None = None
    direct_client: httpx.AsyncClient | None = None
    cookies: str | None = None
    flaresolverr_session_ids: list[str] = []
    flaresolverr_session_queue: asyncio.Queue[str] | None = None
    init_lock: asyncio.Lock | None = None
    ongoing_episode_cache_ttl = timedelta(hours=ONGOING_EPISODE_CACHE_TTL_HOURS)
    finished_episode_cache_ttl = timedelta(hours=FINISHED_EPISODE_CACHE_TTL_HOURS)
    ongoing_translation_cache_ttl = timedelta(hours=ONGOING_TRANSLATION_CACHE_TTL_HOURS)
    finished_translation_cache_ttl = timedelta(hours=FINISHED_TRANSLATION_CACHE_TTL_HOURS)
    video_cache_ttl = timedelta(hours=VIDEO_CACHE_TTL_HOURS)
    anime_status_cache_ttl = timedelta(hours=ANIME_STATUS_CACHE_TTL_HOURS)
    finished_stable_after = timedelta(days=FINISHED_STABLE_AFTER_DAYS)
    video_probe_timeout = VIDEO_PROBE_TIMEOUT_SECONDS
    pending_status_prefetches: set[str] = set()
    inflight_parsing_lock: asyncio.Lock | None = None
    inflight_parsing_requests: dict[ParsingRequestKey, asyncio.Task[dict[str, list[TranslationSchema]]]] = {}
    anilist_status_query = '''
query ($search: String!) {
    Page(page: 1, perPage: 8) {
        media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
            format
            status
            endDate {
                year
                month
                day
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

    @classmethod
    def get_scrape_url(cls, url: str):
        if url.startswith('/'):
            url = SA_HOST + url
        if not url.startswith(('https://', 'http://')):
            url = 'https://' + url
        scrape_url = f"https://api.scrape.do/?token={SCRAPE_TOKEN}&url={url}&sessionId=900"
        if cls.cookies is not None:
            scrape_url += f"&setCookies={urllib.parse.quote(cls.cookies)}"
        return scrape_url

    @classmethod
    def _get_proxy_provider(cls) -> str:
        provider = PARSING_PROXY_PROVIDER.casefold().strip()
        if provider not in {'scrapedo', 'flaresolverr'}:
            raise ValueError(f'Unsupported parsing proxy provider: {PARSING_PROXY_PROVIDER}')
        return provider

    @classmethod
    def _get_flaresolverr_url(cls) -> str:
        host = FLARESOLVERR_HOST.strip()
        if not host:
            raise ValueError('FLARESOLVERR_HOST must not be empty')
        if not host.startswith(('http://', 'https://')):
            host = f'http://{host}'
        return f'{host.rstrip("/")}:{FLARESOLVERR_PORT}/v1'

    @classmethod
    def _get_init_lock(cls) -> asyncio.Lock:
        if cls.init_lock is None:
            cls.init_lock = asyncio.Lock()
        return cls.init_lock

    @classmethod
    def _get_direct_client(cls) -> httpx.AsyncClient:
        if cls.direct_client is None:
            cls.direct_client = httpx.AsyncClient(
                timeout=cls.video_probe_timeout,
                follow_redirects=True,
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            )
        return cls.direct_client

    @classmethod
    def _get_inflight_parsing_lock(cls) -> asyncio.Lock:
        if cls.inflight_parsing_lock is None:
            cls.inflight_parsing_lock = asyncio.Lock()
        return cls.inflight_parsing_lock

    @classmethod
    def _get_flaresolverr_session_queue(cls) -> asyncio.Queue[str]:
        if cls.flaresolverr_session_queue is None:
            cls.flaresolverr_session_queue = asyncio.Queue()
        return cls.flaresolverr_session_queue

    @classmethod
    def _get_flaresolverr_session_pool_size(cls) -> int:
        return max(1, int(FLARESOLVERR_SESSION_POOL_SIZE))

    @classmethod
    async def _proxy_get(cls, url: str) -> ProxyResponse:
        return await SmotretAnimeProxyClient.get(url)

    @classmethod
    async def _proxy_post(cls, url: str, data: dict[str, str]) -> ProxyResponse:
        return await SmotretAnimeProxyClient.post(url, data)

    @classmethod
    async def _flaresolverr_request(
        cls,
        command: str,
        url: str,
        *,
        post_data: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ProxyResponse:
        session_id = await cls._get_flaresolverr_session_queue().get()
        try:
            return await cls._flaresolverr_request_with_session(
                session_id,
                command,
                url,
                post_data=post_data,
                headers=headers,
            )
        finally:
            cls._get_flaresolverr_session_queue().put_nowait(session_id)

    @classmethod
    async def _flaresolverr_request_with_session(
        cls,
        session_id: str,
        command: str,
        url: str,
        *,
        post_data: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ProxyResponse:
        normalized_url = cls._normalize_url(url)
        payload: dict[str, object] = {
            'cmd': command,
            'url': normalized_url,
            'maxTimeout': FLARESOLVERR_MAX_TIMEOUT_MS,
            'session': session_id,
        }
        if post_data is not None:
            payload['postData'] = post_data
        if headers is not None:
            payload['headers'] = headers

        response = await cls.client.post(cls._get_flaresolverr_url(), json=payload)
        _raise_for_status_with_context(
            response,
            f'FlareSolverr request failed: session_id={session_id} command={command} target_url={normalized_url}',
        )

        body = response.json()
        if body.get('status') != 'ok':
            message = body.get('message') or 'Unknown FlareSolverr error'
            raise RuntimeError(f'FlareSolverr request failed: {message}')

        solution = body.get('solution') or {}
        solution_headers = solution.get('headers') or {}
        normalized_headers = {str(key): str(value) for key, value in solution_headers.items()}
        return ProxyResponse(
            text=solution.get('response') or '',
            status_code=int(solution.get('status') or 0),
            headers=normalized_headers,
        )

    @classmethod
    def _extract_login_csrf(cls, html: str) -> str:
        soup = cls._parse_html(html)
        csrf_input = soup.select_one('input[name=csrf]')
        if csrf_input is None:
            raise RuntimeError('Unable to find SmotretAnime login CSRF token')
        csrf = csrf_input.get('value')
        if not isinstance(csrf, str) or not csrf:
            raise RuntimeError('SmotretAnime login CSRF token is empty')
        return csrf

    @classmethod
    async def _login_flaresolverr_session(cls, session_id: str) -> None:
        login_page = await cls._flaresolverr_request_with_session(session_id, 'request.get', '/users/login')
        csrf = cls._extract_login_csrf(login_page.text)
        data = {
            'csrf': csrf,
            'LoginForm[username]': LOGIN,
            'LoginForm[password]': PASSWORD,
            'yt0': '',
            'dynpage': '0',
        }
        login_response = await cls._flaresolverr_request_with_session(
            session_id,
            'request.post',
            '/users/login',
            post_data=urllib.parse.urlencode(data),
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        if login_response.status_code != 200:
            raise RuntimeError(f'FlareSolverr login failed with status {login_response.status_code}')

    @classmethod
    async def _create_flaresolverr_session(cls) -> str:
        response = await cls.client.post(
            cls._get_flaresolverr_url(),
            json={'cmd': 'sessions.create'},
        )
        _raise_for_status_with_context(response, 'FlareSolverr session create failed')

        body = response.json()
        if body.get('status') != 'ok':
            message = body.get('message') or 'Unknown FlareSolverr error'
            raise RuntimeError(f'FlareSolverr session create failed: {message}')

        session_id = body.get('session')
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError('FlareSolverr did not return a valid session id')
        return session_id

    @classmethod
    async def _destroy_flaresolverr_sessions(cls) -> None:
        if cls.client is None or not cls.flaresolverr_session_ids:
            return
        try:
            for session_id in cls.flaresolverr_session_ids:
                await cls.client.post(
                    cls._get_flaresolverr_url(),
                    json={'cmd': 'sessions.destroy', 'session': session_id},
                )
        except httpx.HTTPError:
            logger.warning('Failed to destroy one or more FlareSolverr sessions during shutdown', exc_info=True)
        finally:
            cls.flaresolverr_session_ids = []
            cls.flaresolverr_session_queue = None

    @classmethod
    async def get_streams(
        cls,
        titles: str | Sequence[str],
        episode_number: int,
        base_url: str,
        quality_profile: str,
        external_search_urls: Sequence[str] | None = None,
    ) -> dict[str, list[dict[str, str]]]:
        try:
            translations = await cls.get_by_titles(titles, episode_number, external_search_urls=external_search_urls)
            streams: list[dict[str, str]] = []
            addon_name = f'SmotretAnime{cls._normalize_quality_profile(quality_profile).upper()}'

            for translation_type, label in (('dub', 'Dub'), ('sub', 'Sub')):
                for translation in translations[translation_type]:
                    streams.append(
                        {
                            'name': addon_name,
                            'title': f'{label}: {translation.name}',
                            'url': cls._build_video_endpoint(base_url, translation.url, quality_profile),
                            'behaviorHints': {
                                'bingeGroup': cls._build_binge_group(translation),
                            },
                        }
                    )

            return {'streams': streams}
        except Exception:
            logger.exception(
                'Failed to build streams: episode_number=%s quality_profile=%s titles=%s external_search_urls=%s',
                episode_number,
                quality_profile,
                cls._normalize_titles(titles),
                cls._normalize_titles(external_search_urls or []),
            )
            raise

    @classmethod
    async def get_video_redirect_url(cls, translation_url: str, quality_profile: str) -> str:
        try:
            quality_profile = cls._normalize_quality_profile(quality_profile)
            video = await cls._get_cached_video(translation_url, quality_profile)
            if video is not None and await cls._is_video_url_available(video.video_url):
                return video.video_url

            if cls.client is None:
                await cls.init()

            video = await cls._fetch_video(translation_url, quality_profile)
            await cls._save_video_cache(translation_url, quality_profile, video.video_url)

            return video.video_url
        except Exception:
            logger.exception(
                'Failed to resolve video redirect: translation_url=%s quality_profile=%s',
                translation_url,
                quality_profile,
            )
            raise
    
    @classmethod
    async def get_by_title(cls, title: str, episode_number: int) -> dict:
        return await cls.get_by_titles([title], episode_number)

    @classmethod
    async def get_by_titles(
        cls,
        titles: str | Sequence[str],
        episode_number: int,
        external_search_urls: Sequence[str] | None = None,
    ) -> dict:
        normalized_titles = cls._normalize_titles(titles)
        normalized_external_urls = cls._normalize_titles(external_search_urls or [])

        for title in normalized_titles:
            episode_ttl, translation_ttl = await cls._get_parsing_cache_ttls(title)
            cached_episode = await cls._get_cached_episode(title, episode_number, episode_ttl, translation_ttl)
            if cached_episode is not None:
                return cls._translations_payload_from_models(cached_episode.translations)

        if cls.client is None:
            await cls.init()

        return await cls._get_by_titles_deduplicated(
            normalized_titles,
            episode_number,
            normalized_external_urls,
        )

    @classmethod
    def _clone_translations_payload(
        cls,
        translations: dict[str, list[TranslationSchema]],
    ) -> dict[str, list[TranslationSchema]]:
        return {
            'dub': [item.model_copy(deep=True) for item in translations.get('dub', [])],
            'sub': [item.model_copy(deep=True) for item in translations.get('sub', [])],
        }

    @classmethod
    def _register_parsing_cleanup(
        cls,
        request_key: ParsingRequestKey,
        task: asyncio.Task[dict[str, list[TranslationSchema]]],
    ) -> None:
        def cleanup(_: asyncio.Task[dict[str, list[TranslationSchema]]]) -> None:
            current_task = cls.inflight_parsing_requests.get(request_key)
            if current_task is task:
                cls.inflight_parsing_requests.pop(request_key, None)

        task.add_done_callback(cleanup)

    @classmethod
    async def _get_by_titles_deduplicated(
        cls,
        normalized_titles: list[str],
        episode_number: int,
        normalized_external_urls: list[str],
    ) -> dict[str, list[TranslationSchema]]:
        request_key = ParsingRequestKey(
            episode_number=episode_number,
            titles=tuple(normalized_titles),
            external_search_urls=tuple(normalized_external_urls),
        )

        async with cls._get_inflight_parsing_lock():
            task = cls.inflight_parsing_requests.get(request_key)
            if task is None:
                task = asyncio.create_task(
                    cls._resolve_translations_for_titles(
                        normalized_titles,
                        episode_number,
                        normalized_external_urls,
                    )
                )
                cls.inflight_parsing_requests[request_key] = task
                cls._register_parsing_cleanup(request_key, task)

        try:
            return cls._clone_translations_payload(await asyncio.shield(task))
        except Exception:
            logger.exception(
                'Failed to resolve translations for request: episode_number=%s titles=%s external_search_urls=%s',
                episode_number,
                normalized_titles,
                normalized_external_urls,
            )
            raise

    @classmethod
    async def _resolve_translations_for_titles(
        cls,
        normalized_titles: list[str],
        episode_number: int,
        normalized_external_urls: list[str],
    ) -> dict[str, list[TranslationSchema]]:
        cache_title = normalized_titles[0] if normalized_titles else None

        for search_url in normalized_external_urls:
            episodes = await cls._get_episodes(search_url)
            episode = next((item for item in episodes if item.number == episode_number), None)
            if episode is None:
                continue

            translations = await cls._get_episode_translations(episode.url)
            if cache_title is not None:
                await cls._save_episode_cache(cache_title, episode.number, episode.url, translations)
            return translations

        for title in normalized_titles:
            episodes = await cls._get_episodes(title)
            episode = next((item for item in episodes if item.number == episode_number), None)
            if episode is None:
                continue

            translations = await cls._get_episode_translations(episode.url)
            await cls._save_episode_cache(title, episode.number, episode.url, translations)
            return translations

        return {'dub': [], 'sub': []}
    
    @classmethod
    async def _get_episodes(cls, title: str) -> list[EpisodeSchema]:
        anime_soup = await cls._get_anime_search_result_soup(title)
        if anime_soup is None:
            return []
        episodes: list[EpisodeSchema] = []
        for episode in anime_soup.select('a.m-episode-item[href]'):
            href = episode.get('href')
            if not cls._is_supported_episode_href(href):
                continue
            number = cls._extract_episode_number(href)
            if href is None or number is None:
                continue
            episodes.append(EpisodeSchema(url=href, number=number))
        return episodes

    @classmethod
    async def _get_anime_search_result_soup(cls, query: str) -> BeautifulSoup | None:
        search_query = urllib.parse.quote(query, safe=':/?=&')
        response = await cls._proxy_get(f'/catalog/search?q={search_query}&dynpage=1')
        response_soup = cls._parse_html(response.text)

        if cls._is_direct_anime_page(response_soup):
            return response_soup

        anime_link = cls._extract_anime_link(response_soup)
        if anime_link is None:
            return None

        anime_response = await cls._proxy_get(anime_link)
        return cls._parse_html(anime_response.text)

    @classmethod
    def _is_direct_anime_page(cls, soup: BeautifulSoup) -> bool:
        return soup.select_one('a.m-episode-item[href]') is not None
    
    @classmethod
    async def _get_episode_translations(cls, episode_url: str) -> dict[str, list[TranslationSchema]]:
        task_dub = cls._proxy_get(episode_url)
        task_sub = cls._proxy_get(episode_url + '/russkie-subtitry')
        r_dub, r_sub = await asyncio.gather(task_dub, task_sub)

        translations = {
            'dub': cls._extract_translations(cls._parse_html(r_dub.text), 'dub'),
            'sub': cls._extract_translations(cls._parse_html(r_sub.text), 'sub'),
        }
        return translations

    @classmethod
    async def _fetch_video(cls, translation_url: str, quality_profile: str) -> VideoSchema:
        video_url = await cls._resolve_available_video_url(translation_url, quality_profile)
        if video_url is None:
            raise ValueError(f'Unable to extract video URL for translation: {translation_url}')
        return VideoSchema(translation_url=translation_url, quality_profile=quality_profile, video_url=video_url)

    @classmethod
    async def _get_cached_episode(
        cls,
        title: str,
        episode_number: int,
        episode_ttl: timedelta,
        translation_ttl: timedelta,
    ) -> SmotretAnimeEpisodeCache | None:
        async with get_session() as session:
            statement = (
                select(SmotretAnimeEpisodeCache)
                .options(selectinload(SmotretAnimeEpisodeCache.translations))
                .where(
                    SmotretAnimeEpisodeCache.anime_title == title,
                    SmotretAnimeEpisodeCache.episode_number == episode_number,
                )
            )
            episode_cache = await session.scalar(statement)
            if episode_cache is None:
                return None
            if not cls._is_cache_fresh(episode_cache, episode_ttl):
                return None
            if not cls._are_translations_fresh(episode_cache.translations, translation_ttl):
                return None
            return episode_cache

    @classmethod
    async def _get_parsing_cache_ttls(cls, title: str) -> tuple[timedelta, timedelta]:
        if await cls._uses_ongoing_cache_profile(title):
            return cls.ongoing_episode_cache_ttl, cls.ongoing_translation_cache_ttl
        return cls.finished_episode_cache_ttl, cls.finished_translation_cache_ttl

    @classmethod
    async def _uses_ongoing_cache_profile(cls, title: str) -> bool:
        cached_status = await cls._get_cached_anime_release_status(title)
        if cached_status is None:
            cls._schedule_anime_release_status_prefetch(title)
            return True
        return cls._is_ongoing_or_recently_finished(cached_status.is_ongoing, cached_status.finished_at)

    @classmethod
    def _schedule_anime_release_status_prefetch(cls, title: str) -> None:
        normalized_title = cls._normalize_status_title(title)
        if not normalized_title or normalized_title in cls.pending_status_prefetches:
            return
        cls.pending_status_prefetches.add(normalized_title)
        asyncio.create_task(cls._prefetch_anime_release_status(title, normalized_title))

    @classmethod
    async def _prefetch_anime_release_status(cls, title: str, normalized_title: str) -> None:
        try:
            await cls._fetch_and_cache_anime_release_status(title)
        except Exception:
            logger.warning(
                'Failed to prefetch anime release status: title=%s normalized_title=%s',
                title,
                normalized_title,
                exc_info=True,
            )
        finally:
            cls.pending_status_prefetches.discard(normalized_title)

    @classmethod
    async def _get_cached_anime_release_status(cls, title: str) -> AnimeReleaseStatusCache | None:
        normalized_title = cls._normalize_status_title(title)
        if not normalized_title:
            return None

        async with get_session() as session:
            statement = select(AnimeReleaseStatusCache).where(
                AnimeReleaseStatusCache.normalized_title == normalized_title,
            )
            cached_status = await session.scalar(statement)
            if cached_status is None:
                return None
            if not cls._is_cache_fresh(cached_status, cls.anime_status_cache_ttl):
                return None
            return cached_status

    @classmethod
    async def _fetch_and_cache_anime_release_status(cls, title: str) -> AnimeReleaseStatusCache | None:
        normalized_title = cls._normalize_status_title(title)
        if not normalized_title:
            return None

        if cls.client is None:
            await cls.init()

        response = await cls.client.post(
            'https://graphql.anilist.co',
            json={'query': cls.anilist_status_query, 'variables': {'search': title}},
            headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
        )
        if response.status_code != 200:
            return None

        media_items = response.json().get('data', {}).get('Page', {}).get('media', [])
        best_match = cls._select_best_release_status_match(title, media_items)
        if best_match is None:
            return None

        is_ongoing = cls._media_is_ongoing(best_match)
        finished_at = cls._parse_anilist_end_date(best_match.get('endDate') or {})
        return await cls._save_anime_release_status_cache(normalized_title, is_ongoing, finished_at)

    @classmethod
    async def _save_anime_release_status_cache(
        cls,
        normalized_title: str,
        is_ongoing: bool,
        finished_at: datetime | None,
    ) -> AnimeReleaseStatusCache:
        async with get_session() as session:
            statement = select(AnimeReleaseStatusCache).where(
                AnimeReleaseStatusCache.normalized_title == normalized_title,
            )
            cached_status = await session.scalar(statement)

            if cached_status is None:
                cached_status = AnimeReleaseStatusCache(
                    normalized_title=normalized_title,
                    is_ongoing=is_ongoing,
                    finished_at=finished_at,
                    created_at=cls._utcnow(),
                    updated_at=cls._utcnow(),
                )
                session.add(cached_status)
            else:
                cached_status.is_ongoing = is_ongoing
                cached_status.finished_at = finished_at
                cached_status.updated_at = cls._utcnow()

            await session.commit()
            return cached_status

    @classmethod
    async def _save_episode_cache(
        cls,
        title: str,
        episode_number: int,
        episode_url: str,
        translations: dict[str, list[TranslationSchema]],
    ) -> None:
        async with get_session() as session:
            statement = (
                select(SmotretAnimeEpisodeCache)
                .options(selectinload(SmotretAnimeEpisodeCache.translations))
                .where(
                    SmotretAnimeEpisodeCache.anime_title == title,
                    SmotretAnimeEpisodeCache.episode_number == episode_number,
                )
            )
            episode_cache = await session.scalar(statement)

            if episode_cache is None:
                episode_cache = SmotretAnimeEpisodeCache(
                    anime_title=title,
                    episode_number=episode_number,
                    episode_url=episode_url,
                    created_at=cls._utcnow(),
                    updated_at=cls._utcnow(),
                )
                session.add(episode_cache)
                await session.flush()
            else:
                episode_cache.episode_url = episode_url
                episode_cache.updated_at = cls._utcnow()

            await session.execute(
                delete(SmotretAnimeTranslationCache).where(
                    SmotretAnimeTranslationCache.episode_cache_id == episode_cache.id,
                )
            )

            for translation_type, items in translations.items():
                for item in items:
                    session.add(
                        SmotretAnimeTranslationCache(
                            episode_cache_id=episode_cache.id,
                            translation_type=translation_type,
                            translation_name=item.name,
                            translation_url=item.url,
                        )
                    )

            await session.commit()

    @classmethod
    async def _get_cached_video(cls, translation_url: str, quality_profile: str) -> VideoSchema | None:
        async with get_session() as session:
            statement = select(SmotretAnimeVideoCache).where(
                SmotretAnimeVideoCache.translation_url == translation_url,
                SmotretAnimeVideoCache.quality_profile == quality_profile,
            )
            video_cache = await session.scalar(statement)
            if video_cache is None or not cls._is_cache_fresh(video_cache, cls.video_cache_ttl):
                return None
            if not cls._is_direct_video_url(video_cache.video_url):
                return None
            return VideoSchema(
                translation_url=video_cache.translation_url,
                quality_profile=quality_profile,
                video_url=video_cache.video_url,
            )

    @classmethod
    async def _save_video_cache(cls, translation_url: str, quality_profile: str, video_url: str) -> None:
        async with get_session() as session:
            statement = select(SmotretAnimeVideoCache).where(
                SmotretAnimeVideoCache.translation_url == translation_url,
                SmotretAnimeVideoCache.quality_profile == quality_profile,
            )
            video_cache = await session.scalar(statement)

            if video_cache is None:
                session.add(
                    SmotretAnimeVideoCache(
                        translation_url=translation_url,
                        quality_profile=quality_profile,
                        video_url=video_url,
                        created_at=cls._utcnow(),
                        updated_at=cls._utcnow(),
                    )
                )
            else:
                video_cache.video_url = video_url
                video_cache.updated_at = cls._utcnow()

            await session.commit()

    @classmethod
    async def clear_episode_cache(cls, titles: str | Sequence[str], episode_number: int) -> dict[str, int]:
        normalized_titles = cls._normalize_titles(titles)
        if not normalized_titles:
            return {'episodes': 0, 'translations': 0, 'videos': 0}

        async with get_session() as session:
            statement = (
                select(SmotretAnimeEpisodeCache)
                .options(selectinload(SmotretAnimeEpisodeCache.translations))
                .where(
                    SmotretAnimeEpisodeCache.anime_title.in_(normalized_titles),
                    SmotretAnimeEpisodeCache.episode_number == episode_number,
                )
            )
            episode_caches = list((await session.scalars(statement)).all())
            if not episode_caches:
                return {'episodes': 0, 'translations': 0, 'videos': 0}

            episode_ids = [episode_cache.id for episode_cache in episode_caches]
            translation_urls = [
                translation.translation_url
                for episode_cache in episode_caches
                for translation in episode_cache.translations
            ]

            deleted_videos = 0
            if translation_urls:
                delete_videos_result = await session.execute(
                    delete(SmotretAnimeVideoCache).where(
                        SmotretAnimeVideoCache.translation_url.in_(translation_urls),
                    )
                )
                deleted_videos = int(delete_videos_result.rowcount or 0)

            deleted_translations = sum(len(episode_cache.translations) for episode_cache in episode_caches)
            delete_episodes_result = await session.execute(
                delete(SmotretAnimeEpisodeCache).where(
                    SmotretAnimeEpisodeCache.id.in_(episode_ids),
                )
            )
            deleted_episodes = int(delete_episodes_result.rowcount or 0)

            await session.commit()

        logger.info(
            'Cleared episode cache: episode_number=%s titles=%s episodes=%s translations=%s videos=%s',
            episode_number,
            normalized_titles,
            deleted_episodes,
            deleted_translations,
            deleted_videos,
        )
        return {
            'episodes': deleted_episodes,
            'translations': deleted_translations,
            'videos': deleted_videos,
        }

    @classmethod
    def _translations_payload_from_models(
        cls,
        translations: list[SmotretAnimeTranslationCache],
    ) -> dict[str, list[TranslationSchema]]:
        payload: dict[str, list[TranslationSchema]] = {'dub': [], 'sub': []}
        for translation in translations:
            if translation.translation_type not in payload:
                continue
            payload[translation.translation_type].append(
                TranslationSchema(
                    type=translation.translation_type,
                    name=translation.translation_name,
                    url=translation.translation_url,
                )
            )
        for items in payload.values():
            items.sort(key=lambda item: item.name.lower())
        return payload

    @classmethod
    def _extract_translations(
        cls,
        soup: BeautifulSoup,
        translation_type: Literal['sub', 'dub'],
    ) -> list[TranslationSchema]:
        selector = 'a.truncate[href*="/ozvuchka-"]' if translation_type == 'dub' else 'a.truncate[href*="/russkie-subtitry-"]'
        translations: list[TranslationSchema] = []
        seen_urls: set[str] = set()

        for link in soup.select(selector):
            href = link.get('href')
            name = link.get_text(' ', strip=True)
            if href is None or href in seen_urls:
                continue
            if not cls._is_valid_translation_name(name):
                continue
            seen_urls.add(href)
            translations.append(TranslationSchema(type=translation_type, name=name, url=href))

        translations.sort(key=lambda item: item.name.lower())
        return translations

    @classmethod
    def _is_valid_translation_name(cls, name: str) -> bool:
        normalized = re.sub(r'\s+', ' ', name).strip()
        if not normalized:
            return False

        if normalized.isdigit():
            return False

        lowered = normalized.casefold()
        if lowered in {'загрузить еще', 'load more', 'more'}:
            return False

        return True

    @classmethod
    async def _resolve_available_video_url(cls, translation_url: str, quality_profile: str) -> str | None:
        for candidate in await cls._extract_video_urls(translation_url, quality_profile):
            if await cls._is_video_url_available(candidate):
                return candidate
        return None

    @classmethod
    async def _extract_video_url(cls, url: str, quality_profile: str = 'fhd') -> str | None:
        video_urls = await cls._extract_video_urls(url, quality_profile)
        return video_urls[0] if video_urls else None

    @classmethod
    async def _extract_video_urls(cls, url: str, quality_profile: str) -> list[str]:
        embed_url = '/translations/embed/' + url.split('/')[-1].split('-')[-1]
        embed_response = await cls._proxy_get(embed_url)
        embed_soup = cls._parse_html(embed_response.text)
        return cls._extract_video_urls_from_embed(embed_soup, quality_profile)

    @classmethod
    def _extract_embed_url(cls, soup: BeautifulSoup) -> str | None:
        for selector in (
            'meta[property="og:video"]',
            'meta[property="og:video:url"]',
            'meta[property="og:video:iframe"]',
        ):
            node = soup.select_one(selector)
            if node is None:
                continue
            value = node.get('content')
            if value:
                return cls._normalize_url(value)
        return None

    @classmethod
    def _extract_video_url_from_embed(cls, soup: BeautifulSoup, quality_profile: str = 'fhd') -> str | None:
        video_urls = cls._extract_video_urls_from_embed(soup, quality_profile)
        return video_urls[0] if video_urls else None

    @classmethod
    def _extract_video_urls_from_embed(cls, soup: BeautifulSoup, quality_profile: str) -> list[str]:
        video = soup.select_one('video[data-sources]')
        if video is None:
            return []

        data_sources = video.get('data-sources')
        if not data_sources:
            return []

        try:
            sources = json.loads(data_sources)
        except json.JSONDecodeError:
            logger.warning(
                'Failed to decode embed video sources JSON: quality_profile=%s data_sources=%s',
                quality_profile,
                data_sources[:200],
                exc_info=True,
            )
            return []

        candidates: list[tuple[int, str]] = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            height = source.get('height') or 0
            urls = source.get('urls') or []
            if not isinstance(urls, list) or not urls:
                continue
            candidate = urls[0]
            if not isinstance(candidate, str) or not candidate:
                continue
            candidates.append((int(height), cls._normalize_url(candidate)))

        max_height = cls.quality_max_height[cls._normalize_quality_profile(quality_profile)]
        filtered_candidates = [item for item in candidates if item[0] <= max_height]
        if filtered_candidates:
            candidates = filtered_candidates
        else:
            candidates.sort(key=lambda item: item[0])
            candidates = candidates[:1]

        candidates.sort(key=lambda item: item[0], reverse=True)

        unique_urls: list[str] = []
        seen_urls: set[str] = set()
        for _height, candidate in candidates:
            if candidate in seen_urls:
                continue
            seen_urls.add(candidate)
            unique_urls.append(candidate)

        return unique_urls

    @classmethod
    def _extract_anime_link(cls, soup: BeautifulSoup) -> str | None:
        anime_link = soup.select_one('.m-catalog-item h2 a[href], .m-catalog-item h3 a[href]')
        return anime_link.get('href') if anime_link is not None else None

    @classmethod
    def _extract_episode_number(cls, href: str | None) -> int | None:
        if href is None:
            return None
        match = re.search(r'(?:^|[-/])(\d+)-seriya(?:-|$)', href)
        if match is None:
            return None
        return int(match.group(1))

    @classmethod
    def _is_supported_episode_href(cls, href: str | None) -> bool:
        if href is None:
            return False
        if '/treyler-' in href:
            return False
        return '-seriya' in href

    @classmethod
    def _build_video_endpoint(cls, base_url: str, translation_url: str, quality_profile: str) -> str:
        encoded_translation_url = urllib.parse.quote(translation_url, safe='')
        return f'{base_url.rstrip("/")}/{cls._normalize_quality_profile(quality_profile)}/video?translation_url={encoded_translation_url}'

    @classmethod
    def _normalize_quality_profile(cls, quality_profile: str) -> str:
        normalized = quality_profile.casefold().strip()
        if normalized not in cls.quality_max_height:
            raise ValueError(f'Unsupported quality profile: {quality_profile}')
        return normalized

    @classmethod
    def _build_binge_group(cls, translation: TranslationSchema) -> str:
        release_group = cls._extract_release_group(translation.name)
        normalized_name = cls._normalize_translation_group_name(translation.name)
        encoded_name = urllib.parse.quote(normalized_name, safe='')
        return f'smotretanime-{translation.type}-{release_group}-{encoded_name}'

    @classmethod
    def _extract_release_group(cls, translation_name: str) -> str:
        match = re.search(r'[\(\[]\s*(bd|dvd|tv)\s*[\)\]]', translation_name, flags=re.IGNORECASE)
        if match is not None:
            return match.group(1).lower()

        match = re.search(r'(?:^|\s|-)(bd|dvd|tv)(?:$|\s)', translation_name, flags=re.IGNORECASE)
        if match is not None:
            return match.group(1).lower()

        return 'default'

    @classmethod
    def _normalize_translation_group_name(cls, translation_name: str) -> str:
        normalized = translation_name.casefold()
        normalized = re.sub(r'[\(\[]\s*(bd|dvd|tv)\s*[\)\]]', ' ', normalized, flags=re.IGNORECASE)
        normalized = re.sub(r'(?<!\w)(bd|dvd|tv)(?!\w)', ' ', normalized, flags=re.IGNORECASE)
        normalized = re.sub(r'[._+/,&-]+', ' ', normalized)
        normalized = re.sub(r'[^\w\s]+', ' ', normalized, flags=re.UNICODE)
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        return normalized or 'unknown'

    @classmethod
    def _normalize_titles(cls, titles: str | Sequence[str]) -> list[str]:
        if isinstance(titles, str):
            candidates = [titles]
        else:
            candidates = list(titles)

        normalized: list[str] = []
        seen: set[str] = set()
        for title in candidates:
            if not isinstance(title, str):
                continue
            value = title.strip()
            if not value:
                continue
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(value)
        return normalized

    @classmethod
    def _normalize_url(cls, url: str) -> str:
        if url.startswith('/'):
            return f'https://{SA_HOST}{url}'
        if not url.startswith(('https://', 'http://')):
            return f'https://{url}'
        return url

    @classmethod
    def _is_direct_video_url(cls, url: str) -> bool:
        return '/translations/mp4Stream/' not in url and '/translations/embed/' not in url

    @classmethod
    async def _is_video_url_available(cls, url: str) -> bool:
        try:
            async with cls._get_direct_client().stream('GET', url, headers={'Range': 'bytes=0-0'}) as response:
                status_code = response.status_code
                content_type = (response.headers.get('content-type') or '').casefold()
        except httpx.HTTPError:
            logger.warning('Video probe failed: url=%s', url, exc_info=True)
            return False

        if status_code not in {200, 206}:
            return False

        if content_type and any(marker in content_type for marker in ('text/html', 'application/json')):
            return False

        return True

    @classmethod
    def _is_cache_fresh(cls, cache_entry: SmotretAnimeEpisodeCache | SmotretAnimeTranslationCache | SmotretAnimeVideoCache, ttl: timedelta) -> bool:
        cached_at = cache_entry.updated_at or cache_entry.created_at
        if cached_at is None:
            return False

        now = cls._utcnow()
        if cached_at.tzinfo is None:
            now = now.replace(tzinfo=None)

        return now - cached_at <= ttl

    @classmethod
    def _are_translations_fresh(cls, translations: list[SmotretAnimeTranslationCache], translation_ttl: timedelta) -> bool:
        return all(cls._is_cache_fresh(translation, translation_ttl) for translation in translations)

    @classmethod
    def _is_ongoing_or_recently_finished(cls, is_ongoing: bool, finished_at: datetime | None) -> bool:
        if is_ongoing:
            return True
        if finished_at is None:
            return True

        now = cls._utcnow()
        if finished_at.tzinfo is None:
            now = now.replace(tzinfo=None)

        return now - finished_at < cls.finished_stable_after

    @classmethod
    def _select_best_release_status_match(cls, seed_title: str, media_items: list[dict]) -> dict | None:
        if not media_items:
            return None
        return max(media_items, key=lambda item: cls._score_release_status_match(seed_title, item))

    @classmethod
    def _score_release_status_match(cls, seed_title: str, media_item: dict) -> int:
        score = 0
        normalized_seed = cls._normalize_status_title(seed_title)
        candidates = cls._extract_release_status_titles(media_item)
        normalized_candidates = [cls._normalize_status_title(value) for value in candidates if value]

        if normalized_seed in normalized_candidates:
            score += 120
        elif any(normalized_seed and normalized_seed in value for value in normalized_candidates):
            score += 45
        elif any(value and value in normalized_seed for value in normalized_candidates):
            score += 35

        media_format = (media_item.get('format') or '').upper()
        if media_format == 'TV':
            score += 20
        elif media_format in {'ONA', 'OVA', 'TV_SHORT'}:
            score += 10

        return score

    @classmethod
    def _extract_release_status_titles(cls, media_item: dict) -> list[str]:
        titles: list[str] = []
        media_title = media_item.get('title') or {}
        for value in (
            media_title.get('english'),
            media_title.get('romaji'),
            media_title.get('native'),
        ):
            if isinstance(value, str):
                titles.append(value)

        synonyms = media_item.get('synonyms') or []
        if isinstance(synonyms, list):
            for value in synonyms:
                if isinstance(value, str):
                    titles.append(value)

        return titles

    @classmethod
    def _media_is_ongoing(cls, media_item: dict) -> bool:
        status = (media_item.get('status') or '').upper()
        return status in {'RELEASING', 'NOT_YET_RELEASED'}

    @classmethod
    def _parse_anilist_end_date(cls, end_date: dict) -> datetime | None:
        if not isinstance(end_date, dict):
            return None

        year = end_date.get('year')
        month = end_date.get('month')
        day = end_date.get('day')
        if not all(isinstance(value, int) and value > 0 for value in (year, month, day)):
            return None

        try:
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            logger.warning('Failed to parse AniList end date: end_date=%s', end_date, exc_info=True)
            return None

    @classmethod
    def _normalize_status_title(cls, title: str) -> str:
        normalized = title.casefold().strip()
        normalized = re.sub(r'[._:]+', ' ', normalized)
        normalized = re.sub(r"[\"'`!?(),\[\]{}]+", ' ', normalized)
        normalized = re.sub(r'\s+', ' ', normalized)
        return normalized.strip()

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
    
    @classmethod
    def _parse_html(cls, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, 'html.parser')

    @classmethod
    async def init(cls):
        async with cls._get_init_lock():
            if cls.client is not None:
                await SmotretAnimeProxyClient.init()
                return {'success': True}

            provider = SmotretAnimeProxyClient._get_proxy_provider()
            timeout = FLARESOLVERR_HTTP_TIMEOUT_SECONDS if provider == 'flaresolverr' else 15.0
            cls.client = httpx.AsyncClient(timeout=timeout)
            cls._get_direct_client()
            return await SmotretAnimeProxyClient.init()

    @classmethod
    async def close(cls) -> None:
        async with cls._get_inflight_parsing_lock():
            inflight_parsing_tasks = list(cls.inflight_parsing_requests.values())
            cls.inflight_parsing_requests = {}

        for task in inflight_parsing_tasks:
            task.cancel()

        await SmotretAnimeProxyClient.close()
        if cls.client is not None:
            await cls.client.aclose()
            cls.client = None
        if cls.direct_client is not None:
            await cls.direct_client.aclose()
            cls.direct_client = None
