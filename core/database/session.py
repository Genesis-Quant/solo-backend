from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from config import DatabaseSettings


def sqlalchemy_database_url() -> URL:
    DatabaseSettings.validate()
    url = make_url(DatabaseSettings.URL)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    if url.username is None:
        url = url.set(username=DatabaseSettings.USERNAME, password=DatabaseSettings.PASSWORD)
    return url


@lru_cache
def database_engine() -> Engine:
    return create_engine(sqlalchemy_database_url(), pool_pre_ping=True)


@lru_cache
def database_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=database_engine(), autoflush=False, expire_on_commit=False)


def get_database_session() -> Generator[Session, None, None]:
    with database_session_factory()() as session:
        yield session
