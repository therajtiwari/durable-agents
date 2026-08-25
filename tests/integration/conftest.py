import asyncio
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.community.postgres import PostgresContainer

from durable_agents.storage.postgres import PostgresEventStore

MIGRATION_SQL = (
    Path(__file__).resolve().parents[2] / "db" / "migrations" / "001_events_table.sql"
).read_text()


async def _apply_migration(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(MIGRATION_SQL)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    with PostgresContainer("postgres:16") as container:
        # Applied once via a throwaway event loop, outside pytest-asyncio's
        # per-test loop entirely — the container is session-scoped, but an
        # asyncpg connection created here can't be reused later: asyncpg
        # binds a connection to the event loop it was created under, and
        # pytest-asyncio (in strict mode) gives each test its own loop.
        asyncio.run(_apply_migration(container.get_connection_url(driver=None)))
        yield container


@pytest_asyncio.fixture
async def event_store(
    postgres_container: PostgresContainer,
) -> AsyncGenerator[PostgresEventStore, None]:
    dsn = postgres_container.get_connection_url(driver=None)
    pool = await asyncpg.create_pool(dsn)
    await pool.execute("TRUNCATE events")
    try:
        yield PostgresEventStore(pool)
    finally:
        await pool.close()
