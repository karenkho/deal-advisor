"""Alembic environment. Runs as the owner role (DATABASE_URL_OWNER), or on a connection passed in by the tests."""
from alembic import context
from sqlalchemy import create_engine

from app.config import env, load_dotenv_if_present
from app.db import driver_url
from app.models import Base


def run(connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    load_dotenv_if_present()
    url = env("DATABASE_URL_OWNER")
    if not url:
        raise SystemExit("DATABASE_URL_OWNER is not set.")
    with create_engine(driver_url(url)).begin() as conn:
        run(conn)
