"""Answering one question: validate -> lock -> Trendyol -> record the outcome.

Routes stay thin; this is the only place that combines the state machine (crud)
with the Trendyol client. Every path that takes the lock also releases it by moving
the question out of ANSWERING (ANSWERED, FAILED or CLOSED).
"""

import logging

from app import crud
from app.models import Question
from app.trendyol_client import ErrorCode, TrendyolError, validate_answer_text

logger = logging.getLogger(__name__)

# Trendyol outcomes after which answering this question can never succeed.
TERMINAL_CODES = (ErrorCode.QUESTION_ALREADY_ANSWERED, ErrorCode.TIME_LIMIT_EXCEEDED)


class AnswerError(Exception):
    """Failure safe to show to the frontend (normalized code + message, no raw body)."""

    def __init__(self, code, message, http_status):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status


def submit_answer(session, client, question_id, text):
    """Returns {"question_id", "local_status", "answer_id"} or raises AnswerError."""
    # 1) Validate before locking: a too-short answer must not burn a lock/FAILED state.
    try:
        text = validate_answer_text(text)
    except TrendyolError as exc:
        raise AnswerError(exc.code, exc.message, 400) from None

    if client is None:
        raise AnswerError("TRENDYOL_NOT_CONFIGURED", "Trendyol API settings are missing.", 503)

    # 2) Claim the question. Only NEW/REOPENED can be claimed, by exactly one request.
    if not crud.try_lock_for_answering(session, question_id):
        current = session.get(Question, question_id)
        if current is None:
            raise AnswerError("QUESTION_NOT_FOUND", "Question not found.", 404)
        raise AnswerError(
            "ALREADY_LOCKED",
            f"Question cannot be answered now (status: {current.local_status}).",
            409,
        )

    # 3) We own ANSWERING from here on: every branch below must move it out again.
    question = session.get(Question, question_id)
    try:
        result = client.send_answer(question.trendyol_question_id, text)
    except TrendyolError as exc:
        session.rollback()
        if exc.code in TERMINAL_CODES:
            crud.mark_closed(session, question_id, exc.code, exc.message)
            logger.info("Question %s closed by Trendyol: %s", question_id, exc.code)
        else:
            crud.mark_failed(session, question_id, exc.code, exc.message, exc.retryable)
            logger.warning("Answer for question %s failed: %s (retryable=%s)",
                           question_id, exc.code, exc.retryable)
        raise AnswerError(exc.code, exc.message, 502 if exc.retryable else 422) from None
    except Exception:
        session.rollback()
        logger.exception("Unexpected error while answering question %s", question_id)
        crud.mark_failed(session, question_id, "INTERNAL_ERROR",
                         "Unexpected error while sending the answer.", retryable=True)
        raise AnswerError("INTERNAL_ERROR", "Unexpected error while sending the answer.",
                          500) from None

    crud.mark_answered(session, question_id, text, result["answer_id"])
    logger.info("Question %s answered (answer_id=%s)", question_id, result["answer_id"])
    return {"question_id": question_id, "local_status": "ANSWERED",
            "answer_id": result["answer_id"]}
