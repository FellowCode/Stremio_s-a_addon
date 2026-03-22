from pathlib import Path
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from settings import DATABASE_URL

engine = create_async_engine(DATABASE_URL, echo=False)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


if DATABASE_URL.startswith('sqlite'):
    @event.listens_for(engine.sync_engine, 'connect')
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.close()


@asynccontextmanager
async def get_session():
    async with SessionFactory() as session:
        yield session


def get_sync_database_url(database_url: str) -> str:
    if '+aiosqlite' in database_url:
        return database_url.replace('+aiosqlite', '')
    if '+asyncpg' in database_url:
        return database_url.replace('+asyncpg', '')
    return database_url


def run_migrations() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    config.set_main_option('sqlalchemy.url', get_sync_database_url(DATABASE_URL))
    command.upgrade(config, 'head')


async def close_engine() -> None:
    await engine.dispose()