from importlib import resources

import asyncpg


def schema_sql() -> str:
    """The DDL this runtime needs, read from the copy that ships inside
    the package.

    Exposed separately from create_schema() for anyone who manages
    migrations themselves (Alembic, Flyway, a DBA with opinions) and
    wants the statements rather than having the library execute them.
    """

    return (resources.files("durable_agents.storage") / "schema.sql").read_text(encoding="utf-8")


async def create_schema(dsn: str) -> None:
    """Create the events table if it doesn't already exist.

    Idempotent, so calling it on every application boot is fine. This
    exists because a library whose wheel contains no way to create its
    own table isn't installable in any practical sense — the SQL used to
    live only in the repo's db/migrations/, which never shipped.
    """

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(schema_sql())
    finally:
        await conn.close()
