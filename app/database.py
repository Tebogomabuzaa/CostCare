import logging
import os
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


log = logging.getLogger(__name__)


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
    if engine.dialect.name == "postgresql":
        _enable_row_level_security()


def _enable_row_level_security() -> None:
    """Block Supabase's public REST API (anon key) from reading CostCare's tables.

    Supabase exposes tables in the public schema through its Data API. Row level security with no
    policies denies that access, while the app still works: it connects as the tables' owner, which
    bypasses RLS. Only tables that don't have RLS yet are altered, so cold starts stay cheap.
    """
    app_tables = {table.name for table in Base.metadata.sorted_tables}
    try:
        with engine.begin() as conn:
            missing = conn.execute(text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relkind = 'r' AND NOT c.relrowsecurity"
            )).scalars().all()
            for name in sorted(set(missing) & app_tables):
                conn.execute(text(f'ALTER TABLE "{name}" ENABLE ROW LEVEL SECURITY'))
                log.info("Enabled row level security on %s", name)
    except Exception as exc:  # never stop the app from starting; surface it in the logs
        log.warning("Could not enable row level security: %s", exc)
