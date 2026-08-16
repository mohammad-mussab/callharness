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
    ("calls", "language", "VARCHAR(32)"),
    ("turns", "stt_ms", "FLOAT"),
    ("turns", "llm_ttft_ms", "FLOAT"),
    ("turns", "tts_ttfb_ms", "FLOAT"),
    ("turns", "translated_text", "TEXT"),
    ("turns", "tool_calls", "JSON"),
    ("calls", "transfer_reason", "VARCHAR(32)"),
    ("calls", "non_completion_reason", "VARCHAR(32)"),
    ("calls", "reason_source", "VARCHAR(16)"),
    ("analysis_config", "output_language", "VARCHAR(32) DEFAULT 'english'"),
    # DEFAULT TRUE (not 1) so the same DDL is valid on both SQLite and Postgres.
    ("analysis_config", "classification_enabled", "BOOLEAN DEFAULT TRUE"),
    ("analysis_config", "transfer_reasons", "JSON"),
    ("analysis_config", "non_completion_reasons", "JSON"),
    # TIMESTAMP, not DATETIME — the latter is a SQLite-ism Postgres rejects.
    ("calls", "log_blob", "VARCHAR(512)"),
    ("calls", "log_checked_at", "TIMESTAMP"),
    # The single call-classification layer (see buckets.py). Note ALTER TABLE adds the
    # column but not the index declared on the model — as is already true of
    # transfer_reason above, an index only exists on databases create_all() built from
    # scratch. Fine at current volumes; revisit if `?bucket=` filtering gets slow.
    ("calls", "bucket", "VARCHAR(32)"),
    ("calls", "issue_note", "TEXT"),
    ("calls", "unanswered_query", "TEXT"),
    ("analysis_config", "buckets", "JSON"),
    ("analysis_config", "bucketing_enabled", "BOOLEAN DEFAULT TRUE"),
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
