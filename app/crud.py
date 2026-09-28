"""Question persistence and state transitions.

Every function here is one unit of work: it runs its statement(s) and commits.
Status transitions are single conditional UPDATEs, so they are safe when several
workers/processes touch the same row concurrently.
"""

from sqlalchemy import case, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Question

# Official Trendyol QnA statuses (developers.trendyol.com, questions/filter).
# Only WAITING_FOR_ANSWER can be answered; every other status is final for us.
# REJECTED means the seller's answer was rejected; the question is not answerable.
TRENDYOL_OPEN = ("WAITING_FOR_ANSWER",)
TRENDYOL_CLOSED = ("ANSWERED", "REJECTED", "REPORTED", "UNANSWERED")

# Local statuses a user may answer (and try_lock_for_answering may claim).
ANSWERABLE_STATUSES = ("NEW", "REOPENED")

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
    """Oldest-first NEW and REOPENED questions (what a user can answer)."""
    stmt = (
        select(Question)
        .where(Question.local_status.in_(ANSWERABLE_STATUSES))
        .order_by(Question.question_created_at.asc().nulls_last(), Question.id)
        .limit(limit)
    )
    return list(session.scalars(stmt))


def try_lock_for_answering(session: Session, question_id):
    """Atomically claim a NEW/REOPENED question. Returns True only for the single winner.

    UPDATE ... WHERE local_status IN ('NEW', 'REOPENED') is check-and-set in one
    statement: a concurrent second UPDATE waits on the row lock, re-checks the WHERE
    against the committed row (now ANSWERING), matches nothing and returns no row.
    """
    stmt = (
        update(Question)
        .where(Question.id == question_id, Question.local_status.in_(ANSWERABLE_STATUSES))
        .values(local_status="ANSWERING")
        .returning(Question.id)
    )
    locked = session.execute(stmt).first() is not None
    session.commit()
    return locked


def mark_answered(session: Session, question_id, answer_text, answer_id=None):
    """ANSWERING -> ANSWERED. Returns False if the question was not ANSWERING."""
    stmt = (
        update(Question)
        .where(Question.id == question_id, Question.local_status == "ANSWERING")
        .values(
            local_status="ANSWERED",
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
    """ANSWERING -> FAILED, incrementing retry_count. Returns False if not ANSWERING."""
    stmt = (
        update(Question)
        .where(Question.id == question_id, Question.local_status == "ANSWERING")
        .values(
            local_status="FAILED",
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
            retry_count=Question.retry_count + 1,
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
            error_code=error_code,
            error_message=error_message,
            retryable=False,
        )
        .returning(Question.id)
    )
    ok = session.execute(stmt).first() is not None
    session.commit()
    return ok
