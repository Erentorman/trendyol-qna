"""Question persistence and state transitions.

Every function here is one unit of work: it runs its statement(s) and commits.
Status transitions are single conditional UPDATEs, so they are safe when several
workers/processes touch the same row concurrently.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import and_, case, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Question

logger = logging.getLogger(__name__)

# Official Trendyol QnA statuses (developers.trendyol.com, questions/filter).
# Only WAITING_FOR_ANSWER can be answered; every other status is final for us.
# REJECTED means the seller's answer was rejected; the question is not answerable.
TRENDYOL_OPEN = ("WAITING_FOR_ANSWER",)
TRENDYOL_CLOSED = ("ANSWERED", "REJECTED", "REPORTED", "UNANSWERED")

# Local statuses a user may always answer. FAILED is answerable only when retryable.
ANSWERABLE_STATUSES = ("NEW", "REOPENED")

STALE_ANSWERING_CODE = "STALE_ANSWERING"
STALE_ANSWERING_MESSAGE = (
    "Answering did not finish (process crash or timeout); the question can be retried."
)


def _answerable():
    """SQL condition for 'a user may claim this question now'. Shared by list and lock."""
    return or_(
        Question.local_status.in_(ANSWERABLE_STATUSES),
        and_(Question.local_status == "FAILED", Question.retryable.is_(True)),
    )


def _utcnow():
    return datetime.now(timezone.utc)

# Columns whose source of truth is Trendyol; upsert refreshes only these.
SOURCE_FIELDS = (
    "trendyol_question_id",
    "supplier_id",
    "product_name",
    "product_main_id",
    "barcode",
    "customer_name",
    "question_text",
    "trendyol_status",
    "question_created_at",
    "raw_payload",
)


def _initial_local_status(trendyol_status):
    return "NEW" if trendyol_status in TRENDYOL_OPEN else "CLOSED"


def upsert_questions(session: Session, items):
    """Insert new questions, refresh changed ones. Returns {"inserted": n, "updated": m}.

    `items` are dicts keyed by SOURCE_FIELDS. Local workflow columns (answer_*, error_*,
    retry_count, answered_at) are never overwritten here.
    """
    # ON CONFLICT cannot touch the same row twice in one statement: dedupe, last wins.
    by_id = {item["trendyol_question_id"]: item for item in items}
    if not by_id:
        return {"inserted": 0, "updated": 0}

    rows = [
        {**{f: item.get(f) for f in SOURCE_FIELDS},
         "local_status": _initial_local_status(item.get("trendyol_status"))}
        for item in by_id.values()
    ]

    stmt = insert(Question).values(rows)
    new = stmt.excluded
    current = Question.__table__.c

    # Trendyol still (or again) waits for an answer, but locally we consider it done or
    # failed: flag it for attention. REOPENED is never turned back into NEW here.
    reopen = new.trendyol_status.in_(TRENDYOL_OPEN) & current.local_status.in_(
        ("ANSWERED", "FAILED", "CLOSED")
    )
    local_status = case(
        # Someone is answering right now: only mark_answered/mark_failed may move it.
        (current.local_status == "ANSWERING", current.local_status),
        # Answered/reported on Trendyol (e.g. from the seller panel): nothing left to do.
        (
            new.trendyol_status.in_(TRENDYOL_CLOSED)
            & current.local_status.not_in(("ANSWERED", "CLOSED")),
            "CLOSED",
        ),
        (reopen, "REOPENED"),
        else_=current.local_status,
    )

    stmt = stmt.on_conflict_do_update(
        index_elements=[Question.trendyol_question_id],
        set_={
            **{f: new[f] for f in SOURCE_FIELDS if f != "trendyol_question_id"},
            "local_status": local_status,
            "updated_at": func.now(),
        },
        # Skip no-op updates so updated_at and the "updated" count stay meaningful.
        # `reopen` must be included: it can fire while Trendyol's data is unchanged.
        where=(
            current.raw_payload.is_distinct_from(new.raw_payload)
            | current.trendyol_status.is_distinct_from(new.trendyol_status)
            | reopen
        ),
    ).returning(
        # xmax = 0 only for freshly inserted tuples; updated ones carry the updating txid.
        literal_column("xmax = 0").label("inserted")
    )

    results = session.execute(stmt).scalars().all()
    session.commit()
    inserted = sum(1 for r in results if r)
    return {"inserted": inserted, "updated": len(results) - inserted}


def get_new_questions(session: Session, limit=50):
    """Oldest-first NEW questions. Reading is not claiming: use try_lock_for_answering()."""
    stmt = (
        select(Question)
        .where(Question.local_status == "NEW")
        .order_by(Question.question_created_at.asc().nulls_last(), Question.id)
        .limit(limit)
    )
    return list(session.scalars(stmt))


def get_answerable_questions(session: Session, limit=50):
    """Oldest-first NEW, REOPENED and retryable FAILED questions (what a user can answer)."""
    stmt = (
        select(Question)
        .where(_answerable())
        .order_by(Question.question_created_at.asc().nulls_last(), Question.id)
        .limit(limit)
    )
    return list(session.scalars(stmt))


def get_answered_questions(session: Session, limit=20):
    """Most recently answered first (read-only; feeds the "Son cevaplananlar" list)."""
    stmt = (
        select(Question)
        .where(Question.local_status == "ANSWERED")
        .order_by(Question.answered_at.desc().nulls_last(), Question.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def try_lock_for_answering(session: Session, question_id):
    """Atomically claim an answerable question. Returns True only for the single winner.

    Claimable: NEW, REOPENED, FAILED with retryable=True. The check and the change are
    one UPDATE: a concurrent second UPDATE waits on the row lock, re-checks the WHERE
    against the committed row (now ANSWERING), matches nothing and returns no row.

    retry_count counts retries: it is incremented when the claimed question's previous
    attempt ended in an error (error_code is still set until an answer succeeds).
    """
    stmt = (
        update(Question)
        .where(Question.id == question_id, _answerable())
        .values(
            local_status="ANSWERING",
            locked_at=_utcnow(),
            retry_count=case(
                (Question.error_code.is_not(None), Question.retry_count + 1),
                else_=Question.retry_count,
            ),
        )
        .returning(Question.retry_count, Question.error_code)
    )
    row = session.execute(stmt).first()
    session.commit()
    if row is not None and row.error_code is not None:
        logger.info("Retrying question %s after %s (retry_count=%s)",
                    question_id, row.error_code, row.retry_count)
    return row is not None


def mark_answered(session: Session, question_id, answer_text, answer_id=None):
    """ANSWERING -> ANSWERED. Returns False if the question was not ANSWERING."""
    stmt = (
        update(Question)
        .where(Question.id == question_id, Question.local_status == "ANSWERING")
        .values(
            local_status="ANSWERED",
            locked_at=None,
            answer_text=answer_text,
            answer_id=answer_id,
            answered_at=func.now(),
            error_code=None,
            error_message=None,
            retryable=False,
        )
        .returning(Question.id)
    )
    ok = session.execute(stmt).first() is not None
    session.commit()
    return ok


def mark_failed(session: Session, question_id, error_code, error_message, retryable):
    """ANSWERING -> FAILED. `retryable` decides whether it may be claimed again.

    retry_count is not touched here; it grows when a retry actually starts (see lock).
    Returns False if the question was not ANSWERING (e.g. already reclaimed).
    """
    stmt = (
        update(Question)
        .where(Question.id == question_id, Question.local_status == "ANSWERING")
        .values(
            local_status="FAILED",
            locked_at=None,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
        )
        .returning(Question.id)
    )
    ok = session.execute(stmt).first() is not None
    session.commit()
    return ok


def mark_closed(session: Session, question_id, error_code, error_message):
    """ANSWERING -> CLOSED for final Trendyol outcomes (e.g. already answered elsewhere,
    time limit expired). Records why, never retried. Returns False if not ANSWERING."""
    stmt = (
        update(Question)
        .where(Question.id == question_id, Question.local_status == "ANSWERING")
        .values(
            local_status="CLOSED",
            locked_at=None,
            error_code=error_code,
            error_message=error_message,
            retryable=False,
        )
        .returning(Question.id)
    )
    ok = session.execute(stmt).first() is not None
    session.commit()
    return ok


def reclaim_stale_answering(session: Session, cutoff):
    """ANSWERING rows locked before `cutoff` -> FAILED (retryable). Returns reclaimed ids.

    Covers a crashed process or a lost DB write after the lock. A single conditional
    UPDATE: safe to run concurrently or repeatedly (a reclaimed row is no longer
    ANSWERING, so a second run matches nothing). Rows with a NULL locked_at predate
    the column and are treated as stale.
    """
    stmt = (
        update(Question)
        .where(
            Question.local_status == "ANSWERING",
            or_(Question.locked_at < cutoff, Question.locked_at.is_(None)),
        )
        .values(
            local_status="FAILED",
            locked_at=None,
            retryable=True,
            error_code=STALE_ANSWERING_CODE,
            error_message=STALE_ANSWERING_MESSAGE,
        )
        .returning(Question.id)
    )
    ids = list(session.execute(stmt).scalars())
    session.commit()
    if ids:
        logger.warning("Reclaimed %s stale ANSWERING question(s): %s", len(ids), ids)
    return ids
