"""PostgreSQL connection: the authoritative store (POC v3). The app connects with the runtime role (DATABASE_URL), which
cannot change the audit trail; migrations use the owner role (DATABASE_URL_OWNER), locally only."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.config import env, load_dotenv_if_present

POOL_SIZE = 5
POOL_OVERFLOW = 5
CONNECT_TIMEOUT_SECONDS = 10


def driver_url(url: str) -> str:
    """postgresql://... (the form psql also accepts) -> the same URL with the psycopg driver."""
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


@lru_cache(maxsize=4)
def make_engine(url: str) -> Engine:
    return create_engine(driver_url(url), pool_size=POOL_SIZE, max_overflow=POOL_OVERFLOW, pool_pre_ping=True,
                         connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS})


def engine(name: str = "DATABASE_URL") -> Engine:
    load_dotenv_if_present()
    url = env(name)
    if not url:
        raise RuntimeError(f"{name} is not set.")
    return make_engine(url)


@contextmanager
def session_scope(bind: Engine | None = None):
    """One transaction: commits on success, rolls everything back on any error."""
    with Session(bind or engine()) as session, session.begin():
        yield session
