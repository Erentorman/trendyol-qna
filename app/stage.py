"""Stage test tools: product lookup, test question creation, manual poll.

Every call goes browser -> this blueprint -> TrendyolClient; credentials never leave
the server. All three tools refuse to run unless TY_BASE_URL is Trendyol Stage.
"""

import logging
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template, request
from sqlalchemy import select

from app import crud
from app.db import SessionLocal
from app.models import PollRun, StageTestQuestion
from app.routes import DISPLAY_TZ, MAX_LIMIT, serialize
from app.scheduler import poll_job
from app.trendyol_client import STAGE_HOST, TrendyolError

logger = logging.getLogger(__name__)

bp = Blueprint("stage", __name__)

QUESTION_DEFAULTS = {"user_id": 4147346, "user_full_name": "Test Kullanıcı",
                     "show_user_name": False, "channel_id": 1}
NOT_TRIED = {"state": "NOT_TRIED", "summary": "henüz denenmedi"}
TEST_QUESTION_LABEL = "Trendyol tarafından oluşturuldu - GET servislerinde henüz görünmüyor"
DETAIL_NOT_VISIBLE_MESSAGE = ("Soru oluşturuldu ancak Trendyol soru sorgulama servislerinde "
                              "henüz erişilebilir değil.")
HISTORY_LIMIT = 20


# --- waiting-question rows --------------------------------------------------

def _safe_https(url):
    return url if isinstance(url, str) and url.startswith("https://") else None


def _answer_text(value):
    if isinstance(value, dict):
        value = value.get("text")
    return value if isinstance(value, str) and value else None


def serialize_waiting(q):
    """Row for the "Cevap bekleyen sorular" table: serialize() plus a whitelist of
    display-only fields read from raw_payload. The payload itself is never sent."""
    raw = q.raw_payload if isinstance(q.raw_payload, dict) else {}
    row = serialize(q)
    row.update({
        "product_main_id": q.product_main_id,
        "web_url": _safe_https(raw.get("webUrl")),
        "image_url": _safe_https(raw.get("imageUrl")),
        "answer": _answer_text(raw.get("answer")),
        "rejected_answer": _answer_text(raw.get("rejectedAnswer")),
        "show_user_name": raw.get("showUserName"),
        "created_local": q.question_created_at.astimezone(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M")
        if q.question_created_at else None,
    })
    return row


# --- connectivity summary ---------------------------------------------------

def _record(key, ok, summary):
    """Remember the last Product GET / CreateQuestion POST outcome (process memory)."""
    status = current_app.extensions.setdefault("stage_connectivity", {})
    status[key] = {"state": "SUCCESS" if ok else "FAILED", "summary": summary,
                   "at": datetime.now(timezone.utc).isoformat()}


def _failure_summary(exc):
    reason = exc.detail or exc.code
    status = f"{exc.status_code} " if exc.status_code else ""
    return f"FAILED - {status}{reason}"[:120]


def _questions_get_status(session):
    # Questions GET = the poll, whether run by the scheduler or manually: use the last PollRun.
    run = session.scalars(select(PollRun).where(PollRun.status != "RUNNING")
                          .order_by(PollRun.id.desc()).limit(1)).first()
    if run is None:
        return NOT_TRIED
    if run.status == "SUCCESS":
        return {"state": "SUCCESS", "summary": "SUCCESS",
                "at": run.finished_at.isoformat() if run.finished_at else None}
    return {"state": "FAILED", "summary": f"FAILED - {run.error_message or 'unknown'}"[:120],
            "at": run.finished_at.isoformat() if run.finished_at else None}


def connectivity(session):
    status = current_app.extensions.get("stage_connectivity", {})
    return {"product_get": status.get("product_get", NOT_TRIED),
            "questions_get": _questions_get_status(session),
            "create_question_post": status.get("create_question_post", NOT_TRIED)}


def respond(session, ok, payload, http_status=200):
    body = {"ok": ok, ("data" if ok else "error"): payload,
            "connectivity": connectivity(session)}
    return jsonify(body), http_status


def _stage_client(session):
    """Returns (client, None) or (None, error response). Nothing is sent when not Stage."""
    client = current_app.extensions.get("trendyol_client")
    if client is None:
        return None, respond(session, False, {"code": "TRENDYOL_NOT_CONFIGURED",
                                              "message": "Trendyol API settings are missing."}, 503)
    if not client.is_stage:
        return None, respond(session, False, {"code": "STAGE_ONLY",
                                              "message": f"TY_BASE_URL must be {STAGE_HOST}."}, 400)
    return client, None


def _trendyol_error(session, exc):
    # Test screen: Trendyol's own (truncated) text is shown on purpose, e.g. "RBAC: access denied".
    return respond(session, False, {"code": exc.code, "message": exc.detail or exc.message,
                                    "http_status": exc.status_code,
                                    "correlation_id": exc.correlation_id}, 502)


def _history(session):
    """Newest first. Debug-only rows: never joined with, or copied into, `questions`."""
    rows = session.scalars(select(StageTestQuestion)
                           .order_by(StageTestQuestion.id.desc()).limit(HISTORY_LIMIT)).all()
    return [_serialize_test_question(r) for r in rows]


def _serialize_test_question(row):
    return {"id": row.id, "question_id": row.trendyol_question_id, "text": row.text,
            "content_id": row.content_id, "user_id": row.user_id,
            "user_full_name": row.user_full_name, "created_at": row.created_at.isoformat(),
            "created_at_local": row.created_at.astimezone(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M:%S"),
            "create_http_status": row.create_http_status, "correlation_id": row.correlation_id,
            "label": TEST_QUESTION_LABEL, "answerable": False}


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _invalid(session, message):
    return respond(session, False, {"code": "INVALID_REQUEST", "message": message}, 400)


# --- routes -----------------------------------------------------------------

@bp.get("/stage")
def page():
    with SessionLocal() as session:
        return render_template("stage.html", defaults=QUESTION_DEFAULTS,
                               connectivity=connectivity(session),
                               test_questions=_history(session), label=TEST_QUESTION_LABEL,
                               questions=[serialize_waiting(q) for q in
                                          crud.get_answerable_questions(session, limit=MAX_LIMIT)])


@bp.post("/api/stage/product")
def product():
    body = request.get_json(silent=True) or {}
    barcode = body.get("barcode") if isinstance(body, dict) else None
    with SessionLocal() as session:
        if not isinstance(barcode, str) or not barcode.strip():
            return _invalid(session, "barcode must be a non-empty string.")
        client, error = _stage_client(session)
        if error:
            return error
        try:
            result = client.get_product_by_barcode(barcode)
        except TrendyolError as exc:
            logger.warning("Stage product GET failed: %s (status=%s, correlation_id=%s)",
                           exc.code, exc.status_code, exc.correlation_id)
            _record("product_get", False, _failure_summary(exc))
            return _trendyol_error(session, exc)
        _record("product_get", True, "SUCCESS")
        return respond(session, True, result)


@bp.post("/api/stage/questions")
def create_question():
    body = request.get_json(silent=True)
    with SessionLocal() as session:
        if not isinstance(body, dict):
            return _invalid(session, "Request body must be a JSON object.")
        body = {**{k: QUESTION_DEFAULTS[k] for k in ("show_user_name", "channel_id")}, **body}
        text, full_name = body.get("text"), body.get("user_full_name")
        checks = [
            (_positive_int(body.get("content_id")), "content_id must be a positive integer."),
            (isinstance(text, str) and text.strip(), "text must be a non-empty string."),
            (_positive_int(body.get("user_id")), "user_id must be a positive integer."),
            (isinstance(full_name, str) and full_name.strip(),
             "user_full_name must be a non-empty string."),
            (isinstance(body.get("show_user_name"), bool), "show_user_name must be true or false."),
            (_positive_int(body.get("channel_id")), "channel_id must be a positive integer."),
        ]
        for valid, message in checks:
            if not valid:
                return _invalid(session, message)
        client, error = _stage_client(session)
        if error:
            return error
        try:
            result = client.create_stage_question(
                content_id=body["content_id"], text=text.strip(), user_id=body["user_id"],
                user_full_name=full_name.strip(), show_user_name=body["show_user_name"],
                channel_id=body["channel_id"])
        except TrendyolError as exc:
            logger.warning("Stage question create failed: %s (status=%s, correlation_id=%s)",
                           exc.code, exc.status_code, exc.correlation_id)
            _record("create_question_post", False, _failure_summary(exc))
            return _trendyol_error(session, exc)
        logger.info("Stage question created: question_id=%s", result["question_id"])
        _record("create_question_post", True, "SUCCESS")
        if result["question_id"] is not None:
            row = StageTestQuestion(
                trendyol_question_id=result["question_id"], text=text.strip(),
                content_id=body["content_id"], user_id=body["user_id"],
                user_full_name=full_name.strip(), create_http_status=result["http_status"],
                correlation_id=result["correlation_id"])
            session.add(row)
            session.commit()
            result = {**result, "test_question": _serialize_test_question(row)}
        return respond(session, True, result)


@bp.get("/api/stage/questions/<int:question_id>")
def question_detail(question_id):
    """Debug view of ONE Trendyol question. Read-only: never touches the questions table."""
    with SessionLocal() as session:
        if question_id < 1:
            return _invalid(session, "question_id must be a positive integer.")
        client, error = _stage_client(session)
        if error:
            return error
        try:
            result = client.get_question(question_id)
        except TrendyolError as exc:
            logger.warning("Stage question GET failed: %s (status=%s, correlation_id=%s)",
                           exc.code, exc.status_code, exc.correlation_id)
            if exc.status_code == 404:
                return respond(session, False, {
                    "code": "QUESTION_NOT_VISIBLE", "message": DETAIL_NOT_VISIBLE_MESSAGE,
                    "http_status": 404, "correlation_id": exc.correlation_id}, 404)
            return _trendyol_error(session, exc)
        question = result["question"]
        raw = question["raw_payload"]  # the full detail object, passed through untouched
        status = question["trendyol_status"]
        created = question["question_created_at"]
        return respond(session, True, {
            "http_status": result["http_status"],
            "correlation_id": result["correlation_id"],
            "question_id": question["trendyol_question_id"],
            "status": status,
            "answerable": status == "WAITING_FOR_ANSWER",
            "text": question["question_text"],
            "creation_date": created.isoformat() if created else None,
            "product_name": question["product_name"],
            "product_main_id": question["product_main_id"],
            "user_name": question["customer_name"],
            "show_user_name": raw.get("showUserName"),
            "web_url": raw.get("webUrl"),
            "image_url": raw.get("imageUrl"),
            "answer": raw.get("answer"),
            "rejected_answer": raw.get("rejectedAnswer"),
            "raw": raw,
        })


@bp.get("/api/stage/test-questions")
def list_test_questions():
    with SessionLocal() as session:
        return respond(session, True, {"test_questions": _history(session)})


@bp.post("/api/stage/poll")
def poll():
    with SessionLocal() as session:
        client, error = _stage_client(session)
        if error:
            return error
        run = poll_job(client, SessionLocal)  # same job the scheduler runs; never raises
        if run is None:
            return respond(session, False, {"code": "INTERNAL_ERROR",
                                            "message": "Poll could not be recorded; see logs."}, 500)
        questions = [serialize_waiting(q) for q in crud.get_answerable_questions(session, limit=MAX_LIMIT)]
        data = {"poll_run": {"id": run.id, "status": run.status,
                             "fetched_count": run.fetched_count,
                             "inserted_count": run.inserted_count,
                             "updated_count": run.updated_count,
                             "error_count": run.error_count, "duration_ms": run.duration_ms,
                             "error_message": run.error_message},
                "questions": questions}
        return respond(session, True, data)
