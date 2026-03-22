# AGENTS.md

This file gives project-specific guidance to AI coding agents working in this repository.

## Scope

These instructions apply to the whole repository.

## Project Purpose

This repository is a FastAPI-based Stremio addon that resolves anime streams through SmotretAnime.

Core responsibilities:

- parse Stremio ids from `kitsu`, `anilist`, and `tt`
- resolve search targets using external metadata APIs
- scrape SmotretAnime using either `scrapedo` or `flaresolverr`
- cache results in SQLite using SQLAlchemy async models
- expose manifest, stream, and video redirect routes

## Important Files

- [main.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/main.py) FastAPI app and startup lifecycle.
- [parsers/sa.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/parsers/sa.py) main parser, proxy transport, parsing deduplication, auth refresh, stream assembly.
- [parsers/stremio.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/parsers/stremio.py) Stremio id parsing and metadata/search target resolution.
- [db/models.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/db/models.py) cache schema.
- [db/session.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/db/session.py) async sessions and Alembic bootstrap.
- [settings.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/settings.py) runtime flags and TTLs.

## Architecture Rules

1. Keep transport/auth/session logic inside `SmotretAnimeProxyClient` in [parsers/sa.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/parsers/sa.py).
2. Keep stream assembly and cache-aware parsing logic inside `SmotretAnimeParser` in [parsers/sa.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/parsers/sa.py).
3. Do not add new direct SmotretAnime HTTP calls outside `SmotretAnimeProxyClient` unless there is a strong reason.
4. Prefer fixing concurrency and caching issues at the root cause instead of bypassing deduplication.
5. Preserve async behavior. Do not introduce blocking I/O on request paths.

## Current Concurrency Contracts

1. Identical upstream proxy requests are deduplicated in `SmotretAnimeProxyClient`.
2. Identical parsing requests for the same anime and episode are deduplicated in `SmotretAnimeParser`.
3. Different parsing requests should still be allowed to run concurrently.
4. Shared in-flight tasks must be cancellation-safe. One cancelled waiter must not cancel the work for other waiters.

When changing these areas, verify both:

- deduplicated requests still coalesce to one upstream operation
- cancellation of one waiter does not break the other waiter

## Authentication Rules

1. SmotretAnime auth may expire during runtime.
2. If parsing returns the login page, the transport should refresh auth and retry once.
3. In `flaresolverr` mode, auth is session-specific.
4. In `scrapedo` mode, cookies are persisted in `cookies.txt`.
5. Startup should authenticate before serving requests.

## Cache Rules

1. Reuse existing cache tables before introducing new ones.
2. If new cache data must survive restarts, store it in SQLite and add an Alembic migration.
3. Keep cache keys aligned with current uniqueness constraints in [db/models.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/db/models.py).
4. Be careful with concurrent writes to unique-constrained cache rows.

## Logging Rules

1. Do not silently swallow parser failures.
2. Use contextual logs with enough fields to identify provider, title, episode, quality profile, or URL.
3. Preserve existing behavior for users where possible, but make failures observable in logs.

## Safe Change Patterns

Preferred patterns:

- extend `SmotretAnimeProxyClient` for transport-specific changes
- extend `SmotretAnimeParser` for parsing/caching behavior
- update `settings.py` for non-secret runtime configuration
- add Alembic migrations for schema changes

Avoid:

- duplicating proxy logic in multiple classes
- bypassing the cache layer for one-off fixes
- adding broad `except Exception: pass` blocks
- changing public route shapes in [main.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/main.py) without clear need

## Validation Checklist For Agents

After changing parsing, transport, or cache behavior, validate at least these:

1. changed files have no diagnostics
2. one known stream id still returns non-empty streams
3. concurrent identical requests still work
4. if relevant, `flaresolverr` startup/login still works

Useful checks include ids like:

- `kitsu:11469:1`
- `tt3114390:1:23`

## Run Commands

Install dependencies:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the server:

```powershell
.venv\Scripts\python.exe main.py
```

## Environment Assumptions

- secrets come from `.env`
- non-secret configuration is in [settings.py](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/settings.py)
- startup runs Alembic migrations automatically

## Documentation Discipline

If you change transport behavior, startup behavior, routes, cache schema, or supported id formats, update [README.md](/c:/Users/Admin/Documents/GitHub/stremio_s-a_addon/README.md) in the same change.