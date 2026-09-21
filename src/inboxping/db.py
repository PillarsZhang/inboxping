from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine, delete, event, func, select
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from inboxping.config import Settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def create_db_engine(settings: Settings) -> Engine:
    database_url = settings.storage.database_url
    if database_url.startswith("sqlite:///"):
        path = database_url.removeprefix("sqlite:///")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
        pool_pre_ping=True,
    )
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


class Database:
    def __init__(self, settings: Settings):
        self.engine = create_db_engine(settings)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def init(self) -> None:
        from inboxping import models  # noqa: F401

        Base.metadata.create_all(self.engine)

    def clear_all_data(self) -> dict[str, int]:
        """Clear runtime data while preserving the database schema."""
        from inboxping.models import (
            AccountState,
            Analysis,
            Event,
            Message,
            Notification,
            Translation,
        )

        models = (
            Notification,
            Translation,
            Analysis,
            Message,
            Event,
            AccountState,
        )
        counts: dict[str, int] = {}
        with self.session() as session:
            for model in models:
                counts[model.__tablename__] = (
                    session.scalar(select(func.count()).select_from(model)) or 0
                )
                session.execute(delete(model))
        return counts

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
