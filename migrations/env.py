from logging.config import fileConfig
import os

from sqlalchemy import engine_from_config
from sqlalchemy import event
from sqlalchemy import pool

from alembic import context
from packages.persistence.models import metadata

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config
if os.environ.get("MJOBS_DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", os.environ["MJOBS_DATABASE_URL"].replace("%", "%%"))

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    if connectable.dialect.name == "sqlite":
        # Python sqlite3 legacy transaction control skips BEGIN for DDL. Emit it
        # explicitly so a failed Alembic revision cannot leave orphaned schema.
        @event.listens_for(connectable, "begin")
        def _begin_sqlite_migration_transaction(connection):
            connection.exec_driver_sql("BEGIN")

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite otherwise leaves applied DDL behind if a revision fails,
            # making retry unsafe while alembic_version still points at parent.
            transactional_ddl=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
