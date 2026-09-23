import logging

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

engine = None
SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False)


def init_db(database_url):
    global engine
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )
    SessionLocal.configure(bind=engine)
    return engine


def check_db():
    """Return True if `SELECT 1` succeeds, False otherwise."""
    if engine is None:
        return False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError as exc:
        # Log only the exception class; the message may contain the DSN.
        logger.warning("Database health check failed: %s", type(exc).__name__)
        return False
