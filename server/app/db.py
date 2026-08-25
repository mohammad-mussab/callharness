import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

logger = logging.getLogger("callharness.db")


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
    # Which missing record a record_missing call belongs to, filled in by the grouping
    # pass rather than by per-call analysis. NULL = not grouped yet.
    ("calls", "gap_group_id", "VARCHAR(64)"),
    ("calls", "gap_group_question", "TEXT"),
    # Where to re-ask a missing-record question, to prove it is really missing
    # (gap_verification.py). The gap_groups and gap_verifications TABLES need no entry
    # here — create_all() below builds missing tables; only new columns on tables that
    # already exist have to be listed.
    ("analysis_config", "lookup_probes", "JSON"),
    # Auto-requeue of failed analyses (analysis/failure_kind.py). Existing failed
    # rows get NULL kind / 0 attempts / NULL next_retry, which the worker reads as
    # "never triaged" — so the 158 calls stranded by the Aug-2026 credit outage
    # become claimable again the first time the worker runs with this deployed.
    # DEFAULT 0 (not NULL) on attempts so the counter can be incremented directly.
    ("calls", "analysis_failure_kind", "VARCHAR(16)"),
    ("calls", "analysis_attempts", "INTEGER DEFAULT 0"),
    ("calls", "analysis_next_retry_at", "TIMESTAMP"),
]

# Columns to remove from existing databases. Separate from COLUMN_MIGRATIONS because
# this direction is destructive and irreversible — there is no Alembic here to roll
# back, so a column only belongs on this list once it is established that nothing
# unique is stored in it.
#
# calls.end_reason qualifies. Across 674 live Lazio calls it held only NULL (397) or
# "transferred" (277), agreed with the `transferred` boolean on every single row, and
# never once carried the "completed" value that outcome.compute_outcome() was checking
# for — so it duplicated a column we already have and changed no outcome, chart or
# filter. Contrast transfer_reason / non_completion_reason, which are FROZEN rather
# than dropped precisely because they do hold history nothing else has.
#
# DROP COLUMN needs SQLite 3.35+ (2021) and any supported Postgres. Wrapped so an
# older SQLite in a dev environment logs and carries on instead of failing startup.
COLUMN_DROPS: list[tuple[str, str]] = [
    ("calls", "end_reason"),
]


async def _apply_column_migrations(conn) -> None:
    dialect = engine.dialect.name

    async def _columns(table: str) -> set[str]:
        if dialect == "sqlite":
            rows = (await conn.exec_driver_sql(f"PRAGMA table_info({table})")).fetchall()
            return {r[1] for r in rows}
        rows = (
            await conn.exec_driver_sql(
                f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'"
            )
        ).fetchall()
        return {r[0] for r in rows}

    for table, column in COLUMN_DROPS:
        existing = await _columns(table)
        if existing and column in existing:
            try:
                await conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column}")
                logger.info("Dropped column %s.%s", table, column)
            except Exception as exc:  # noqa: BLE001 - never block startup on this
                logger.warning("Could not drop %s.%s: %s", table, column, exc)

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
