import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@pytest.fixture(scope="session")
def engine():
    url = os.getenv("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.fail("TEST_DATABASE_URL is not set (see .env.example)")
    # Tests truncate tables: refuse to run against anything but a *_test database.
    if not make_url(url).database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must point to a database whose name ends with _test")

    # Build the schema through the real migrations, so they are tested too.
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    eng = create_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture
def Session(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE questions, poll_runs RESTART IDENTITY"))
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(Session):
    with Session() as s:
        yield s
