import os

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


def _optional_int(name):
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


class Config:
    def __init__(self):
        self.DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
        if not self.DATABASE_URL:
            raise ConfigError("DATABASE_URL is required (see .env.example)")

        # Trendyol settings: defined here, not used until a later phase.
        self.TY_SUPPLIER_ID = os.getenv("TY_SUPPLIER_ID", "").strip() or None
        self.TY_API_KEY = os.getenv("TY_API_KEY", "").strip() or None
        self.TY_API_SECRET = os.getenv("TY_API_SECRET", "").strip() or None
        self.TY_BASE_URL = os.getenv("TY_BASE_URL", "").strip() or None
        self.TY_USER_AGENT = os.getenv("TY_USER_AGENT", "").strip() or None
        self.POLL_INTERVAL_SECONDS = _optional_int("POLL_INTERVAL_SECONDS")

    def __repr__(self):
        # Never expose secrets or the DB password in logs/tracebacks.
        return (
            f"Config(TY_SUPPLIER_ID={self.TY_SUPPLIER_ID!r}, "
            f"TY_API_KEY={'***' if self.TY_API_KEY else None}, "
            f"TY_API_SECRET={'***' if self.TY_API_SECRET else None}, "
            f"POLL_INTERVAL_SECONDS={self.POLL_INTERVAL_SECONDS!r})"
        )
