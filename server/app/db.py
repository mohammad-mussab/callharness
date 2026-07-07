from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# Lightweight additive migrations for existing installs (create_all only
# creates missing tables, it never adds columns to existing ones).
COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("calls", "quality", "JSON"),
    ("calls", "interruption_count", "INTEGER DEFAULT 0"),
    ("turns", "stt_ms", "FLOAT"),
    ("turns", "llm_ttft_ms", "FLOAT"),
    ("turns", "tts_ttfb_ms", "FLOAT"),
]


async def _apply_column_migrations(conn) -> None:
    dialect = engine.dialect.name
    for table, column, ddl in COLUMN_MIGRATIONS:
        if dialect == "sqlite":
            rows = (await conn.exec_driver_sql(f"PRAGMA table_info({table})")).fetchall()
            existing = {r[1] for r in rows}
        else:
            rows = (
                await conn.exec_driver_sql(
                    f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'"
                )
            ).fetchall()
            existing = {r[0] for r in rows}
        if existing and column not in existing:
            await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


async def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_column_migrations(conn)


async def get_session():
    async with SessionLocal() as session:
        yield session
