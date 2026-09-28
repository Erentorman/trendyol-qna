from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

LOCAL_STATUSES = ("NEW", "ANSWERING", "ANSWERED", "FAILED", "CLOSED", "REOPENED")
POLL_STATUSES = ("RUNNING", "SUCCESS", "FAILED")


def _in(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    # Deterministic constraint names so Alembic migrations stay stable.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trendyol_question_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    supplier_id: Mapped[int | None] = mapped_column(BigInteger)

    product_name: Mapped[str | None] = mapped_column(Text)
    product_main_id: Mapped[str | None] = mapped_column(String(255))
    barcode: Mapped[str | None] = mapped_column(String(255))
    customer_name: Mapped[str | None] = mapped_column(String(255))
    question_text: Mapped[str] = mapped_column(Text, nullable=False)

    # trendyol_status mirrors Trendyol; local_status is our own workflow state.
    trendyol_status: Mapped[str | None] = mapped_column(String(50))
    local_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="NEW")

    answer_text: Mapped[str | None] = mapped_column(Text)
    answer_id: Mapped[int | None] = mapped_column(BigInteger)

    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    question_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(_in("local_status", LOCAL_STATUSES), name="local_status"),
        # Serves get_new_questions(): filter by status, oldest first.
        Index("ix_questions_local_status_created", "local_status", "question_created_at"),
    )


class PollRun(Base):
    __tablename__ = "poll_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="RUNNING")
    fetched_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    inserted_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint(_in("status", POLL_STATUSES), name="status"),)
