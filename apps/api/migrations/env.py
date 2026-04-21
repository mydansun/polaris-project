from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from polaris_api.config import get_settings
from polaris_api.db import Base
from polaris_api import models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()


def migration_database_url() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


config.set_main_option("sqlalchemy.url", migration_database_url())
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=migration_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
