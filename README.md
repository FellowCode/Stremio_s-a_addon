# Stremio SmotretAnime Addon

FastAPI addon for Stremio that resolves anime streams through SmotretAnime and returns playable stream entries for `kitsu`, `anilist`, and `tt` ids.

## What It Does

- Exposes Stremio addon manifests and `stream` endpoints.
- Resolves Stremio ids into search targets using Kitsu, AniList, and Cinemeta.
- Enriches Kitsu search targets with MyAnimeList and AniList aliases when mappings are available.
- Searches SmotretAnime by external ids first when possible, then falls back to title search.
- For `tt...` ids, skips non-anime Cinemeta entries and returns an empty stream list instead of searching SmotretAnime.
- Supports multiple quality profiles such as `hd`, `fhd`, and `4k`.
- Caches episode, translation, video, title, release-status, and Kitsu mapping data in SQLite via SQLAlchemy.
- Supports two upstream transport modes for SmotretAnime parsing:
  - `scrapedo`
  - `flaresolverr`

## Project Layout

- [main.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/main.py) FastAPI app, manifests, stream routes, video redirect route, startup lifecycle.
- [settings.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/settings.py) runtime configuration and cache TTLs.
- [parsers/sa.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/parsers/sa.py) SmotretAnime transport, parsing, stream assembly, caches, auth refresh.
- [parsers/stremio.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/parsers/stremio.py) Stremio id parsing and search-target resolution.
- [parsers/kitsu.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/parsers/kitsu.py) Kitsu metadata helpers.
- [db/models.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/db/models.py) SQLAlchemy ORM models for all caches.
- [db/session.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/db/session.py) async engine/session factory and Alembic bootstrap.
- [alembic](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/alembic) database migrations.

## Supported Stream Ids

The addon accepts Stremio ids in these forms:

- `kitsu:<id>:<episode>`
- `kitsu:<id>:<season>:<episode>`
- `anilist:<id>:<episode>`
- `anilist:<id>:<season>:<episode>`
- `tt<imdb_id>:<episode>`
- `tt<imdb_id>:<season>:<episode>`

Movies default to episode `1` when the content type is `movie`.

## Endpoints

- `GET /` returns known manifest URLs.
- `GET /manifest.json` returns the default `fhd` manifest.
- `GET /{quality_profile}/manifest.json` returns a manifest for the requested quality.
- `GET /stream/{type}/{id}.json` uses the default `fhd` profile.
- `GET /{quality_profile}/stream/{type}/{id}.json` returns stream candidates for that quality.
- `GET /reset-cache/{type}/{id}.json` clears cached episode, translation, and related video entries for the selected episode.
- `GET /video?translation_url=...` redirects to the resolved media URL for `fhd`.
- `GET /{quality_profile}/video?translation_url=...` redirects to the resolved media URL for the requested profile.
- `GET /login-sa` triggers parser initialization and login.

## Configuration

Most runtime settings live in [settings.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/settings.py).

Required environment variables:

- `LOGIN` SmotretAnime login.
- `PASSWORD` SmotretAnime password.

Required only when `PARSING_PROXY_PROVIDER = 'scrapedo'`:

- `SCRAPE_TOKEN`

Optional:

- `ALEMBIC_DATABASE_URL`

Important settings in [settings.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/settings.py):

- `PARSING_PROXY_PROVIDER`
- `FLARESOLVERR_HOST`
- `FLARESOLVERR_PORT`
- `FLARESOLVERR_SESSION_POOL_SIZE`
- `FLARESOLVERR_PERSIST_SESSIONS`
- `FLARESOLVERR_SESSION_STATE_FILE`
- `QUALITY_PROFILES`
- cache TTL values for episodes, translations, videos, AniList, Cinemeta, and Kitsu mappings

## Setup

1. Create and activate a virtual environment.
2. Install dependencies.
3. Create a `.env` file with secrets.
4. Start FlareSolverr if you use `flaresolverr` mode.
5. Run the server.

Example `.env`:

```env
LOGIN=your_login
PASSWORD=your_password
SCRAPE_TOKEN=your_scrape_do_token
```

Install dependencies:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the addon:

```powershell
.venv\Scripts\python.exe main.py
```

The app starts on `http://127.0.0.1:8000`.

## Startup Behavior

On startup the application:

1. runs Alembic migrations to `head`
2. initializes the parser
3. authenticates the configured transport

In `flaresolverr` mode, login happens during server startup and each FlareSolverr session in the pool is authenticated before requests are served.

## Caching

SQLite caches live in [db.sqlite3](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/db.sqlite3).

Current cache tables include:

- `sa_episode_cache`
- `sa_translation_cache`
- `sa_video_cache`
- `anilist_title_cache`
- `cinemeta_title_cache`
- `anime_release_status_cache`
- `kitsu_mappings_cache`

Notes:

- episode and translation TTLs adapt based on cached release status
- video cache is separated by `quality_profile`
- Kitsu mappings are persisted to avoid repeated id conversion lookups across restarts

## Transport Notes

`scrapedo`:

- uses a single scrape.do session id with cookie injection
- stores refreshed cookies in `cookies.txt`

`flaresolverr`:

- uses a session pool
- persists session ids to reuse authenticated FlareSolverr browser sessions across addon restarts when `FLARESOLVERR_PERSIST_SESSIONS` is enabled
- automatically reauthenticates if parsing hits the login page again
- concurrent upstream work is limited by `FLARESOLVERR_SESSION_POOL_SIZE`

## Concurrency Model

- identical proxy requests are deduplicated in-flight so only one upstream request runs
- identical parsing requests for the same anime and episode are also deduplicated in-flight
- different anime or different episodes can still run concurrently
- cancellation of one waiting client does not cancel the shared upstream parse or proxy task for other clients

## Development Notes

- The addon labels streams by quality profile, for example `SmotretAnimeFHD` and `SmotretAnime4K`.
- Search prefers external source URLs such as MyAnimeList and AniDB before title search.
- Direct media links are extracted from `video[data-sources]` on SmotretAnime embed pages.
- `web.strem.io` private-network preflight is handled in [main.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/main.py).

## Troubleshooting

If streams are empty:

1. check that `LOGIN` and `PASSWORD` are valid
2. confirm the selected proxy provider is reachable
3. if using `flaresolverr`, confirm the FlareSolverr service is running on the configured host and port
4. inspect parser logs for auth refresh, proxy failures, and stream build failures
5. call `GET /login-sa` to force reinitialization if needed
6. call `GET /reset-cache/{type}/{id}.json` to clear cache for a stuck episode and force a fresh parse on the next request

If schema errors appear:

1. delete stale virtual environment state if necessary
2. ensure Alembic can reach the configured database
3. restart the server so startup migrations run again
