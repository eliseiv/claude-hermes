"""Alembic migration environment (async engine, 07-deployment.md)."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _db_url() -> str:
    """Resolve the SQLAlchemy URL for migrations (TD-008, 09-e2e-testing.md §3.1).

    Priority: the URL passed via the Alembic Config (alembic.ini `sqlalchemy.url` or a value
    injected programmatically through `config`). This lets migrations run against an arbitrary
    DB handed in via Alembic Config (e2e DB / testcontainers) without depending on env load
    order. Fallback ONLY when the Config key is empty/unset: prefer DATABASE_URL_MIGRATE (the
    full-privilege `app_migrate` role for DDL — ADR-053 durable append-only audit_logs), falling
    back to DATABASE_URL when DATABASE_URL_MIGRATE is unset (local single-role / backward-compat),
    so the docker-compose `migrate` job keeps working with either DSN.
    """
    section = config.get_section(config.config_ini_section, {}) or {}
    configured = config.get_main_option("sqlalchemy.url") or section.get("sqlalchemy.url")
    if configured:
        return configured
    settings = get_settings()
    return settings.database_url_migrate or settings.database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:  # noqa: ANN001
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _db_url()
    connectable = async_engine_from_config(configuration, prefix="sqlalchemy.", poolclass=NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
