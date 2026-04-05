from contextlib import asynccontextmanager
import logging
from pathlib import Path
import sys



from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from starlette.datastructures import Headers
from starlette.responses import Response
from db.session import close_engine, run_migrations
from parsers.sa import SmotretAnimeParser
from parsers.stremio import get_stream_search_targets, parse_stream_request_id
from settings import QUALITY_PROFILES


FAVICON_PATH = Path(__file__).resolve().parent / 'static' / 'favicon.ico'
_LOG_FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'


def configure_logging() -> None:
    """Force root logger to INFO / stdout regardless of prior configuration."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(handler)

    for existing in root.manager.loggerDict.values():
        if isinstance(existing, logging.Logger):
            existing.disabled = False

    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('uvicorn.access').setLevel(logging.INFO)


configure_logging()
logger = logging.getLogger(__name__)


class PrivateNetworkCORSMiddleware(CORSMiddleware):
    def preflight_response(self, request_headers: Headers) -> Response:
        response = super().preflight_response(request_headers)
        if request_headers.get('access-control-request-private-network') == 'true':
            headers = dict(response.headers)
            headers['Access-Control-Allow-Private-Network'] = 'true'
            if response.status_code == 400:
                return Response(status_code=200, headers=headers)
            response.headers['Access-Control-Allow-Private-Network'] = 'true'
        return response


def build_manifest(base_url: str, quality_profile: str) -> dict:
    normalized_profile = quality_profile.casefold().strip()
    max_height = QUALITY_PROFILES[normalized_profile]
    quality_suffix = normalized_profile.upper()
    return {
        'id': f'com.fellowcode.smotret-anime.{normalized_profile}',
        'version': '1.0.0',
        'name': f'Smotret-Anime {quality_suffix}',
        'description': f'Smotret-Anime {quality_suffix} addon. Максимальное разрешение видео: {max_height}p.',
        'resources': ['stream'],
        'types': ['movie', 'series', 'anime'],
        'idPrefixes': ['tt', 'kitsu', 'anilist'],
        'logo': f'{base_url.rstrip("/")}/favicon.ico',
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        run_migrations()
        configure_logging()
        await SmotretAnimeParser.init()
    except Exception:
        logger.exception('Application startup failed during lifespan initialization')
        raise

    logger.info('Application startup complete. Parser initialized. Addon is ready on http://127.0.0.1:8000')
    lifespan_failed = False
    try:
        yield
    except Exception:
        lifespan_failed = True
        logger.exception('Application lifespan failed while serving requests')
        raise
    finally:
        shutdown_error: Exception | None = None

        try:
            await SmotretAnimeParser.close()
        except Exception as exc:
            shutdown_error = exc
            logger.exception('Application shutdown failed while closing parser')

        try:
            await close_engine()
        except Exception as exc:
            if shutdown_error is None:
                shutdown_error = exc
            logger.exception('Application shutdown failed while closing database engine')

        if shutdown_error is not None and not lifespan_failed:
            raise shutdown_error


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    PrivateNetworkCORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root(request: Request):
    base_url = str(request.base_url).rstrip('/')
    return {
        'manifests': {
            'fhd': f'{base_url}/fhd/manifest.json',
            'hd': f'{base_url}/hd/manifest.json',
        }
    }


@app.get("/manifest.json")
async def manifest(request: Request):
    return build_manifest(str(request.base_url), 'fhd')


@app.get('/{quality_profile}/manifest.json')
async def quality_manifest(quality_profile: str, request: Request):
    try:
        normalized_profile = SmotretAnimeParser._normalize_quality_profile(quality_profile)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return build_manifest(str(request.base_url), normalized_profile)


@app.get('/favicon.ico')
async def favicon():
    return FileResponse(FAVICON_PATH, media_type='image/x-icon')


@app.get("/stream/{type}/{id}.json")
async def stream(type: str, id: str, request: Request):
    return await quality_stream('fhd', type, id, request)


@app.get('/{quality_profile}/stream/{type}/{id}.json')
async def quality_stream(quality_profile: str, type: str, id: str, request: Request):
    try:
        normalized_profile = SmotretAnimeParser._normalize_quality_profile(quality_profile)
        stream_request = parse_stream_request_id(type, id)
        search_targets = await get_stream_search_targets(type, stream_request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return await SmotretAnimeParser.get_streams(
        search_targets.titles,
        stream_request.episode_number,
        str(request.base_url),
        normalized_profile,
        external_search_urls=search_targets.external_urls,
    )


@app.get('/video', name='video_redirect')
async def video_redirect(translation_url: str):
    return await quality_video_redirect('fhd', translation_url)


@app.get('/reset-cache/{type}/{id}.json')
async def reset_episode_cache(type: str, id: str):
    try:
        stream_request = parse_stream_request_id(type, id)
        search_targets = await get_stream_search_targets(type, stream_request)
        cleared = await SmotretAnimeParser.clear_episode_cache(
            search_targets.titles,
            stream_request.episode_number,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        'reset': True,
        'type': type,
        'id': id,
        'episode_number': stream_request.episode_number,
        'titles': search_targets.titles,
        'cleared': cleared,
    }


@app.get('/{quality_profile}/video', name='quality_video_redirect')
async def quality_video_redirect(quality_profile: str, translation_url: str):
    try:
        normalized_profile = SmotretAnimeParser._normalize_quality_profile(quality_profile)
        redirect_url = await SmotretAnimeParser.get_video_redirect_url(translation_url, normalized_profile)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(url=redirect_url, status_code=307)

@app.get('/login-sa')
async def login_sa():
    return await SmotretAnimeParser.init()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)