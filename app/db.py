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


# Columns added after the first production deploy. create_all() only creates
# missing TABLES — it never alters existing ones — so each new column needs an
# explicit ADD COLUMN. Idempotent: skipped when the column already exists.
_COLUMN_MIGRATIONS = [
    # (table, column, sqlite_ddl, postgres_ddl)
    ("appointments", "insurance_enc", "BLOB", "BYTEA"),
    ("appointments", "reminder_sent", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
    ("calls", "booking_verified", "BOOLEAN", "BOOLEAN"),
    # patient registry + multi-clinic (Jul 2026)
    ("appointments", "clinic_id", "INTEGER DEFAULT 1", "INTEGER DEFAULT 1"),
    ("appointments", "patient_id", "INTEGER", "INTEGER"),
    ("appointments", "status", "VARCHAR DEFAULT 'confirmed'", "VARCHAR DEFAULT 'confirmed'"),
    ("calls", "patient_id", "INTEGER", "INTEGER"),
    ("calls", "clinic_id", "INTEGER DEFAULT 1", "INTEGER DEFAULT 1"),
    # JWT auth (Jul 2026)
    ("dashboard_users", "password_changed_at", "DATETIME", "TIMESTAMPTZ"),
]

# appointments.patient_id and appointments.status were added Jul 2026 alongside
# the patient registry. Postgres can retype patient_id if it was previously VARCHAR.
_TYPE_FIXES_PG = [
    ("appointments", "patient_id",
     "ALTER TABLE appointments ALTER COLUMN patient_id "
     "TYPE INTEGER USING NULLIF(patient_id, '')::integer"),
]


def _ensure_columns(engine: Engine) -> None:
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    is_sqlite = engine.dialect.name == "sqlite"
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, col, sqlite_ddl, pg_ddl in _COLUMN_MIGRATIONS:
            if table not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            if col in existing:
                continue
            ddl = sqlite_ddl if is_sqlite else pg_ddl
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
        if not is_sqlite:
            for table, col, stmt in _TYPE_FIXES_PG:
                cols = {c["name"]: c for c in insp.get_columns(table)} \
                    if table in tables else {}
                info = cols.get(col)
                if info is not None and "INT" not in str(info["type"]).upper():
                    conn.execute(text(stmt))


def _bootstrap_clinic(engine: Engine) -> None:
    """Ensure clinic row #1 exists (single-clinic default from env)."""
    import os
    from sqlalchemy.orm import Session
    from .models import Clinic
    with Session(engine) as session:
        if session.get(Clinic, 1) is None:
            session.add(Clinic(
                id=1,
                name=os.getenv("CLINIC_NAME", "Bright Smile Dental & Aesthetics"),
                timezone=os.getenv("CLINIC_TZ", "America/Indiana/Indianapolis")))
            session.commit()


def init_db(engine: Engine) -> None:
    from .models import Base
    Base.metadata.create_all(engine)
    _ensure_columns(engine)
    _bootstrap_clinic(engine)
