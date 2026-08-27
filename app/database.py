"""Database engine and session management.

Defaults to a local SQLite file so the project runs with zero setup.
Set DATABASE_URL to a Postgres DSN for anything beyond local dev, e.g.:
    postgresql+psycopg2://user:pass@localhost:5432/energy_costs
"""
import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./energy_costs.db")
IS_SQLITE = DATABASE_URL.startswith("sqlite")

connect_args = {"check_same_thread": False} if IS_SQLITE else {}

# pool_pre_ping / pool_recycle matter specifically for Neon (and any
# serverless/autosuspending Postgres): Neon can silently close idle
# connections when its compute scales to zero. Without these, SQLAlchemy's
# pool hands out a connection it thinks is fine, the query fails or hangs
# on a dead socket, and only then does it reconnect — adding real latency
# (or an outright error) on the first request after any idle period.
# pool_pre_ping issues a cheap check before each checkout and transparently
# reconnects if needed; pool_recycle proactively retires connections before
# they'd likely go stale in the first place. Harmless no-ops on SQLite.
engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
    pool_recycle=280,  # a bit under Neon's ~300s idle-suspend window
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency: yields a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope():
    """For use outside request handlers, e.g. the scheduler job."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
