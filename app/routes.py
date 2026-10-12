import logging
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, jsonify, render_template, request

from app import crud
from app.answer_service import AnswerError, submit_answer
from app.db import SessionLocal
from app.trendyol_client import ANSWER_MAX_LENGTH, ANSWER_MIN_LENGTH

logger = logging.getLogger(__name__)

bp = Blueprint("questions", __name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
ANSWERED_LIMIT = 20


DISPLAY_TZ = ZoneInfo("Europe/Istanbul")  # DB stores UTC; the UI shows Turkish local time


@bp.app_template_filter("local_dt")
def local_dt(value, fmt="%d.%m.%Y %H:%M"):
    return value.astimezone(DISPLAY_TZ).strftime(fmt) if value else ""


def ok(data, status=200):
    return jsonify(ok=True, data=data), status


def fail(code, message, status, **extra):
    return jsonify(ok=False, error={"code": code, "message": message, **extra}), status


def serialize(q):
    # Explicit whitelist: raw_payload and internal columns never reach the frontend.
    return {
        "id": q.id,
        "trendyol_question_id": q.trendyol_question_id,
        "product_name": q.product_name,
        "customer_name": q.customer_name,
        "question_text": q.question_text,
        "trendyol_status": q.trendyol_status,
        "local_status": q.local_status,
        "retry_count": q.retry_count,
        "retryable": q.retryable,
        "error_code": q.error_code,
        "error_message": q.error_message,
        "question_created_at": q.question_created_at.isoformat() if q.question_created_at else None,
        # Previous answer: relevant for REOPENED questions.
        "answer_text": q.answer_text,
        "answered_at": q.answered_at.isoformat() if q.answered_at else None,
    }


@bp.get("/")
def index():
    with SessionLocal() as session:
        questions = crud.get_answerable_questions(session, limit=MAX_LIMIT)
        answered = crud.get_answered_questions(session, limit=ANSWERED_LIMIT)
        return render_template("index.html", questions=questions, answered=answered,
                               min_length=ANSWER_MIN_LENGTH, max_length=ANSWER_MAX_LENGTH)


@bp.get("/api/questions")
def list_questions():
    limit = request.args.get("limit", DEFAULT_LIMIT, type=int)
    if limit is None or not 1 <= limit <= MAX_LIMIT:
        return fail("INVALID_REQUEST", f"limit must be between 1 and {MAX_LIMIT}.", 400)
    with SessionLocal() as session:
        questions = crud.get_answerable_questions(session, limit=limit)
        return ok([serialize(q) for q in questions])


@bp.post("/api/answer")
def answer():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return fail("INVALID_REQUEST", "Request body must be a JSON object.", 400)
    question_id = body.get("question_id")
    # "answer" is the documented field; "text" is still accepted for older clients.
    text = body.get("answer", body.get("text"))
    # bool is an int subclass: reject true/false explicitly.
    if not isinstance(question_id, int) or isinstance(question_id, bool) or question_id < 1:
        return fail("INVALID_REQUEST", "question_id must be a positive integer.", 400)
    if not isinstance(text, str):
        return fail("INVALID_REQUEST", "answer must be a string.", 400)

    client = current_app.extensions.get("trendyol_client")
    with SessionLocal() as session:
        try:
            return ok(submit_answer(session, client, question_id, text))
        except AnswerError as exc:
            # retryable lets the UI offer "try again" on the same card.
            return fail(exc.code, exc.message, exc.http_status, retryable=exc.retryable)


def register_error_handlers(app):
    """Normalized JSON errors for /api/*; HTML routes keep Flask's default pages."""
    from werkzeug.exceptions import HTTPException

    @app.errorhandler(Exception)
    def handle_error(exc):
        is_http = isinstance(exc, HTTPException)
        if not request.path.startswith("/api/"):
            return exc if is_http else ("Internal Server Error", 500)
        if is_http:
            code = (exc.name or "HTTP_ERROR").upper().replace(" ", "_")
            return fail(code, exc.description or exc.name, exc.code)
        logger.exception("Unhandled error on %s %s", request.method, request.path)
        return fail("INTERNAL_ERROR", "Unexpected server error.", 500)
