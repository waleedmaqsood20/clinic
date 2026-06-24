"""
Database engine and session factory.

  make_engine(url)             -> SQLAlchemy Engine
  make_session_factory(engine) -> sessionmaker (use as: with factory() as session:)
  init_db(engine)              -> create all tables idempotently
"""
from __future__ import annotations

from sqlalchemy import create_engine, Engine
from sqlalchemy.orm import sessionmaker


def make_engine(url: str) -> Engine:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def make_session_factory(engine: Engine):
    return sessionmaker(engine, autocommit=False, autoflush=False,
                        expire_on_commit=False)


def init_db(engine: Engine) -> None:
    from .models import Base
    Base.metadata.create_all(engine)
