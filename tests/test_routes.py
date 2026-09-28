import os
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import select

from app import create_app
from app.models import Question
from app.trendyol_client import ErrorCode, TrendyolClient, TrendyolError
from tests.test_trendyol_client import FakeAdapter

VALID = "Evet, bu ürün pamukludur."


class FakeTrendyol:
    """Replaces TrendyolClient.send_answer; optional gate blocks mid-request."""

    def __init__(self, result=None, error=None, gate=None):
        self.result = result or {"answer_id": 555}
        self.error = error
        self.gate = gate
        self.entered = threading.Event()
        self.calls = []

    def send_answer(self, question_id, text):
        self.calls.append((question_id, text))
        self.entered.set()
        if self.gate:
            self.gate.wait(5)
        if self.error:
            raise self.error
        return {"question_id": question_id, "text": text, **self.result}


@pytest.fixture
def make_app(Session):
    config = SimpleNamespace(DATABASE_URL=os.environ["TEST_DATABASE_URL"], TY_SUPPLIER_ID=None,
                             TY_API_KEY=None, TY_API_SECRET=None, TY_BASE_URL=None,
                             TY_USER_AGENT=None, POLL_INTERVAL_SECONDS=None)

    def _make(client):
        app = create_app(config=config, trendyol_client=client)
        app.testing = True
        return app

    return _make


def add(Session, status="NEW", tid=1000, text="Pamuklu mu?"):
    with Session() as s:
        q = Question(trendyol_question_id=tid, question_text=text, local_status=status,
                     trendyol_status="WAITING_FOR_ANSWER",
                     question_created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
        s.add(q)
        s.commit()
        return q.id


def load(Session, qid):
    with Session() as s:
        return s.get(Question, qid)


def post(app, qid, text=VALID):
    return app.test_client().post("/api/answer", json={"question_id": qid, "answer": text})


def test_index_lists_only_new_and_reopened(Session, make_app):
    ids = {st: add(Session, st, tid=i) for i, st in enumerate(
        ["NEW", "REOPENED", "ANSWERED", "FAILED", "CLOSED", "ANSWERING"])}
    html = make_app(FakeTrendyol()).test_client().get("/").get_data(as_text=True)
    shown = {st for st, qid in ids.items() if f'data-question-id="{qid}"' in html}
    assert shown == {"NEW", "REOPENED"}


def test_index_escapes_customer_text(Session, make_app):
    add(Session, text="<script>alert(1)</script>")
    html = make_app(FakeTrendyol()).test_client().get("/").get_data(as_text=True)
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


def test_api_questions_format(Session, make_app):
    qid = add(Session, "REOPENED")
    add(Session, "ANSWERED", tid=2)
    resp = make_app(FakeTrendyol()).test_client().get("/api/questions")
    body = resp.get_json()
    assert resp.status_code == 200 and body["ok"] is True
    assert [q["id"] for q in body["data"]] == [qid]
    assert body["data"][0]["local_status"] == "REOPENED"
    assert "raw_payload" not in body["data"][0]


def test_api_questions_bad_limit(make_app, Session):
    body = make_app(None).test_client().get("/api/questions?limit=0").get_json()
    assert body == {"ok": False, "error": {"code": "INVALID_REQUEST",
                                           "message": "limit must be between 1 and 200."}}


def test_valid_answer_marks_answered(Session, make_app):
    qid = add(Session)
    fake = FakeTrendyol()
    resp = post(make_app(fake), qid, "  " + VALID + "  ")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "data": {"question_id": qid, "local_status": "ANSWERED",
                                                    "answer_id": 555}}
    assert fake.calls == [(1000, VALID)]  # Trendyol id, stripped text
    q = load(Session, qid)
    assert (q.local_status, q.answer_text, q.answer_id) == ("ANSWERED", VALID, 555)
    assert q.answered_at is not None


def test_reopened_is_answerable(Session, make_app):
    qid = add(Session, "REOPENED")
    assert post(make_app(FakeTrendyol()), qid).status_code == 200
    assert load(Session, qid).local_status == "ANSWERED"


def test_short_answer_rejected_without_lock_or_api_call(Session, make_app):
    qid = add(Session)
    fake = FakeTrendyol()
    resp = post(make_app(fake), qid, "kısa")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == ErrorCode.ANSWER_TOO_SHORT
    assert fake.calls == [] and load(Session, qid).local_status == "NEW"


@pytest.mark.parametrize("payload", [None, {"answer": VALID}, {"question_id": "1", "answer": VALID},
                                     {"question_id": True, "answer": VALID},
                                     {"question_id": 1, "answer": 5}, {"question_id": 1}])
def test_invalid_request(make_app, Session, payload):
    resp = make_app(FakeTrendyol()).test_client().post("/api/answer", json=payload)
    assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("status", ["ANSWERING", "ANSWERED", "CLOSED", "FAILED"])
def test_not_answerable_states_return_already_locked(Session, make_app, status):
    qid = add(Session, status)  # FAILED here has retryable=False (column default)
    fake = FakeTrendyol()
    resp = post(make_app(fake), qid)
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "ALREADY_LOCKED"
    assert fake.calls == [] and load(Session, qid).local_status == status


def test_unknown_question(make_app, Session):
    resp = post(make_app(FakeTrendyol()), 999999)
    assert resp.status_code == 404 and resp.get_json()["error"]["code"] == "QUESTION_NOT_FOUND"


def test_retryable_error_marks_failed_retryable(Session, make_app):
    qid = add(Session)
    err = TrendyolError(ErrorCode.SERVER_ERROR, retryable=True, status_code=503)
    resp = post(make_app(FakeTrendyol(error=err)), qid)
    assert resp.status_code == 502
    assert resp.get_json()["error"] == {"code": "SERVER_ERROR", "message": err.message,
                                        "retryable": True}
    q = load(Session, qid)
    assert (q.local_status, q.retryable, q.error_code, q.retry_count, q.locked_at) == (
        "FAILED", True, "SERVER_ERROR", 0, None)


def test_non_retryable_error_marks_failed(Session, make_app):
    qid = add(Session)
    err = TrendyolError(ErrorCode.FORBIDDEN_WORD, status_code=400)
    resp = post(make_app(FakeTrendyol(error=err)), qid)
    assert resp.status_code == 422
    assert resp.get_json()["error"]["retryable"] is False
    q = load(Session, qid)
    assert (q.local_status, q.retryable, q.error_code) == ("FAILED", False, "FORBIDDEN_WORD")


def test_already_answered_closes_question_end_to_end(Session, make_app):
    """Real TrendyolClient + fake transport: raw Turkish body must not reach the response."""
    qid = add(Session)
    raw = {"errors": [{"key": "qna.x", "message": "Soru daha önce cevaplanmış INTERNAL-TRACE-42"}]}
    adapter = FakeAdapter((400, raw))
    http = requests.Session()
    http.mount("https://", adapter)
    client = TrendyolClient(1234, "k", "s", "https://apigw.example", session=http,
                            sleep=lambda s: None)

    resp = post(make_app(client), qid)
    body = resp.get_json()
    assert resp.status_code == 422
    assert body["error"]["code"] == "QUESTION_ALREADY_ANSWERED"
    assert "INTERNAL-TRACE-42" not in resp.get_data(as_text=True)
    assert "cevaplanmış" not in resp.get_data(as_text=True)
    assert len(adapter.calls) == 1

    q = load(Session, qid)
    assert (q.local_status, q.error_code, q.retryable) == ("CLOSED", "QUESTION_ALREADY_ANSWERED", False)
    # No further attempts: a second submit is refused locally.
    assert post(make_app(client), qid).get_json()["error"]["code"] == "ALREADY_LOCKED"
    assert len(adapter.calls) == 1


def test_time_limit_exceeded_closes_question(Session, make_app):
    qid = add(Session)
    post(make_app(FakeTrendyol(error=TrendyolError(ErrorCode.TIME_LIMIT_EXCEEDED))), qid)
    assert load(Session, qid).local_status == "CLOSED"


def test_unexpected_exception_is_normalized_and_releases_lock(Session, make_app):
    qid = add(Session)
    fake = FakeTrendyol(error=RuntimeError('raw body {"secret": "TY-SECRET-VALUE"}'))
    resp = post(make_app(fake), qid)
    assert resp.status_code == 500
    assert resp.get_json() == {"ok": False, "error": {
        "code": "INTERNAL_ERROR", "message": "Unexpected error while sending the answer.",
        "retryable": True}}
    assert "TY-SECRET-VALUE" not in resp.get_data(as_text=True)
    q = load(Session, qid)
    assert (q.local_status, q.retryable, q.error_code) == ("FAILED", True, "INTERNAL_ERROR")


def test_not_configured_client(Session, make_app):
    qid = add(Session)
    resp = post(make_app(None), qid)
    assert resp.status_code == 503
    assert resp.get_json()["error"]["code"] == "TRENDYOL_NOT_CONFIGURED"
    assert load(Session, qid).local_status == "NEW"


def test_unknown_api_route_is_json(make_app, Session):
    resp = make_app(None).test_client().get("/api/nope")
    assert resp.status_code == 404 and resp.get_json()["ok"] is False


def test_concurrent_duplicate_submit_sends_once(Session, make_app):
    qid = add(Session)
    gate = threading.Event()
    fake = FakeTrendyol(gate=gate)
    app = make_app(fake)
    first = {}

    t = threading.Thread(target=lambda: first.setdefault("resp", post(app, qid)))
    t.start()
    assert fake.entered.wait(5)  # first request holds the lock, inside send_answer

    second = post(app, qid)
    assert second.status_code == 409
    assert second.get_json()["error"]["code"] == "ALREADY_LOCKED"

    gate.set()
    t.join(5)
    assert first["resp"].status_code == 200
    assert len(fake.calls) == 1
    # And after success, a resubmit is refused too.
    assert post(app, qid).get_json()["error"]["code"] == "ALREADY_LOCKED"
    assert len(fake.calls) == 1


def test_legacy_text_field_still_accepted(Session, make_app):
    qid = add(Session)
    resp = make_app(FakeTrendyol()).test_client().post(
        "/api/answer", json={"question_id": qid, "text": VALID})
    assert resp.status_code == 200


def test_retryable_failed_can_be_answered_again_via_api(Session, make_app):
    qid = add(Session)
    err = TrendyolError(ErrorCode.NETWORK_ERROR, retryable=True)
    assert post(make_app(FakeTrendyol(error=err)), qid).status_code == 502

    fake = FakeTrendyol()
    resp = post(make_app(fake), qid)
    assert resp.status_code == 200 and len(fake.calls) == 1
    q = load(Session, qid)
    assert (q.local_status, q.retry_count, q.error_code, q.locked_at) == ("ANSWERED", 1, None, None)


def test_api_questions_includes_retryable_failed_only(Session, make_app):
    with Session() as s:
        for tid, retryable in [(1, True), (2, False)]:
            s.add(Question(trendyol_question_id=tid, question_text="?", local_status="FAILED",
                           retryable=retryable, error_code="SERVER_ERROR"))
        s.commit()
    data = make_app(None).test_client().get("/api/questions").get_json()["data"]
    assert [(d["trendyol_question_id"], d["retryable"]) for d in data] == [(1, True)]
