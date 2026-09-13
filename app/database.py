import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    if url.startswith("postgres://"):  # Supabase sometimes shows this legacy scheme
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Supabase's transaction pooler (port 6543) doesn't support prepared statements
        kwargs["connect_args"] = {"prepare_threshold": None}
        if os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
            # Each Lambda instance handles one request at a time; let the pooler manage connections
            kwargs["poolclass"] = NullPool
    return create_engine(url, **kwargs)


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(bind=engine)
