import logging

from flask import Blueprint, current_app, jsonify, render_template, request

from app import crud
from app.answer_service import AnswerError, submit_answer
from app.db import SessionLocal

logger = logging.getLogger(__name__)

bp = Blueprint("questions", __name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def ok(data, status=200):
    return jsonify(ok=True, data=data), status


def fail(code, message, status):
    return jsonify(ok=False, error={"code": code, "message": message}), status


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
        "error_code": q.error_code,
        "error_message": q.error_message,
        "question_created_at": q.question_created_at.isoformat() if q.question_created_at else None,
    }


@bp.get("/")
def index():
    with SessionLocal() as session:
        questions = crud.get_answerable_questions(session, limit=MAX_LIMIT)
        return render_template("index.html", questions=questions)


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
    question_id, text = body.get("question_id"), body.get("text")
    # bool is an int subclass: reject true/false explicitly.
    if not isinstance(question_id, int) or isinstance(question_id, bool) or question_id < 1:
        return fail("INVALID_REQUEST", "question_id must be a positive integer.", 400)
    if not isinstance(text, str):
        return fail("INVALID_REQUEST", "text must be a string.", 400)

    client = current_app.extensions.get("trendyol_client")
    with SessionLocal() as session:
        try:
            return ok(submit_answer(session, client, question_id, text))
        except AnswerError as exc:
            return fail(exc.code, exc.message, exc.http_status)


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
