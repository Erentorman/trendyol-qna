"""Stage test screen. HTTP is faked: these never reach Trendyol."""

import base64
import json
import os
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
import requests

from app import create_app
from app.trendyol_client import ErrorCode, TrendyolClient, TrendyolError
from tests.test_trendyol_client import FakeAdapter

STAGE = "https://stageapigw.trendyol.com"
KEY, SECRET = "stage-KEY-abc123", "stage-SECRET-xyz789"
BASIC = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()

VALID = {"content_id": 1203155798, "text": "Bu ürün hakkında test sorusudur.",
         "user_id": 4147346, "user_full_name": "Test Kullanıcı",
         "show_user_name": False, "channel_id": 1}


def real_client(*replies, base_url=STAGE):
    adapter = FakeAdapter(*replies)
    http = requests.Session()
    http.mount("https://", adapter)
    client = TrendyolClient(1234, KEY, SECRET, base_url, session=http, sleep=lambda s: None)
    return client, adapter


@pytest.fixture
def app_with(Session):
    def _make(client):
        config = SimpleNamespace(DATABASE_URL=os.environ["TEST_DATABASE_URL"],
                                 TY_SUPPLIER_ID="1234", TY_API_KEY=KEY, TY_API_SECRET=SECRET,
                                 TY_BASE_URL=STAGE, TY_USER_AGENT=None, POLL_INTERVAL_SECONDS=None)
        app = create_app(config=config, trendyol_client=client)
        app.testing = True
        return app.test_client()
    return _make


def assert_no_secrets(text):
    for secret in (KEY, SECRET, BASIC, "Authorization"):
        assert secret not in text


# --- page -------------------------------------------------------------------

def test_stage_page_renders_three_tools_and_connectivity(app_with):
    client, _ = real_client()
    resp = app_with(client).get("/stage")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    for text in ("Stage Connectivity", "Product GET:", "Questions GET:", "CreateQuestion POST:",
                 "Ürünü Getir", "Test Sorusu Oluştur", "Test Sorusu Gönder", "Soruları Yenile"):
        assert text in html
    for field in ("barcode", "content_id", "text", "user_id", "user_full_name"):
        assert f'name="{field}"' in html
    assert 'value="4147346"' in html and 'value="Test Kullanıcı"' in html
    for url in ("/api/stage/product", "/api/stage/questions", "/api/stage/poll", "/static/stage.js"):
        assert url in html
    assert html.count("henüz denenmedi") == 3
    assert_no_secrets(html)


def test_stage_js_served_and_has_no_secrets(app_with):
    client, _ = real_client()
    js = app_with(client).get("/static/stage.js").get_data(as_text=True)
    assert "fetch(" in js and "connectivity" in js
    assert_no_secrets(js)


# --- client -----------------------------------------------------------------

def test_create_stage_question_posts_once_with_auth(caplog):
    client, adapter = real_client((200, {"questionId": 98765}, {"X-Correlation-Id": "corr-1"}))
    result = client.create_stage_question(1203155798, "Soru?", 4147346, "Test Kullanıcı", False, 1)
    request, kwargs = adapter.calls[0]
    assert request.method == "POST"
    assert urlparse(request.url).path == "/integration/qna/sellers/1234/questions"
    assert json.loads(request.body) == {"contentId": 1203155798, "text": "Soru?", "userId": 4147346,
                                        "userFullName": "Test Kullanıcı", "showUserName": False,
                                        "channelId": 1}
    assert request.headers["Authorization"] == f"Basic {BASIC}"
    assert request.headers["User-Agent"] == "1234 - SelfIntegration"
    assert kwargs["timeout"] == (5, 20)
    assert result == {"http_status": 200, "question_id": 98765, "correlation_id": "corr-1"}
    assert_no_secrets(caplog.text)


def test_create_stage_question_is_not_retried():
    client, adapter = real_client((503, {"message": "down"}), (200, {"questionId": 1}))
    with pytest.raises(TrendyolError) as exc:
        client.create_stage_question(1, "Soru?", 1, "A", False, 1)
    assert exc.value.code == ErrorCode.SERVER_ERROR and len(adapter.calls) == 1


def test_create_stage_question_refuses_non_stage():
    client, adapter = real_client(base_url="https://apigw.trendyol.com")
    with pytest.raises(ValueError):
        client.create_stage_question(1, "Soru?", 1, "A", False, 1)
    assert adapter.calls == []


# --- route ------------------------------------------------------------------

class FakeStageClient:
    is_stage = True

    def __init__(self, result=None, error=None):
        self.result = result or {"http_status": 200, "question_id": 555, "correlation_id": None}
        self.error = error
        self.calls = []

    def create_stage_question(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def test_valid_request_calls_create_stage_question(app_with):
    fake = FakeStageClient()
    resp = app_with(fake).post("/api/stage/questions", json=VALID)
    body = resp.get_json()
    assert resp.status_code == 200
    data = {k: v for k, v in body["data"].items() if k != "test_question"}
    assert (body["ok"], data) == (True, {"http_status": 200, "question_id": 555,
                                         "correlation_id": None})
    assert body["connectivity"]["create_question_post"]["summary"] == "SUCCESS"
    assert fake.calls == [VALID]


def test_optional_fields_default(app_with):
    fake = FakeStageClient()
    minimal = {k: VALID[k] for k in ("content_id", "text", "user_id", "user_full_name")}
    assert app_with(fake).post("/api/stage/questions", json=minimal).status_code == 200
    assert fake.calls[0]["show_user_name"] is False and fake.calls[0]["channel_id"] == 1


def test_success_end_to_end_returns_question_id(app_with):
    client, _ = real_client((200, {"questionId": 98765}))
    body = app_with(client).post("/api/stage/questions", json=VALID).get_json()
    assert body["ok"] is True and body["data"]["question_id"] == 98765


class TextAdapter(FakeAdapter):
    """Plain-text reply, like a gateway's RBAC denial (FakeAdapter only speaks JSON)."""

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        status, text = self.replies.pop(0)
        resp = requests.Response()
        resp.status_code, resp._content = status, text.encode()
        resp.headers["Content-Type"] = "text/plain"
        resp.request, resp.url = request, request.url
        return resp


def test_403_rbac_is_normalized_and_visible(app_with):
    adapter = TextAdapter((403, "RBAC: access denied"))
    http = requests.Session()
    http.mount("https://", adapter)
    client = TrendyolClient(1234, KEY, SECRET, STAGE, session=http, sleep=lambda s: None)

    resp = app_with(client).post("/api/stage/questions", json=VALID)
    body = resp.get_json()
    assert (body["ok"], body["error"]) == (False, {
        "code": "AUTH_ERROR", "message": "RBAC: access denied",
        "http_status": 403, "correlation_id": None})
    conn = body["connectivity"]["create_question_post"]
    assert (conn["state"], conn["summary"]) == ("FAILED", "FAILED - 403 RBAC: access denied")
    assert len(adapter.calls) == 1  # 403 is never retried
    assert_no_secrets(resp.get_data(as_text=True))


def test_correlation_id_from_error_body_is_returned(app_with):
    client, _ = real_client((400, {"errors": [{"message": "Geçersiz içerik"}],
                                   "correlationId": "abc-123"}))
    body = app_with(client).post("/api/stage/questions", json=VALID).get_json()
    assert body["error"]["correlation_id"] == "abc-123"
    assert body["error"]["http_status"] == 400
    assert body["error"]["message"] == "Geçersiz içerik"


def test_error_response_carries_no_credentials(app_with):
    client, _ = real_client((500, {"message": "internal"}))
    resp = app_with(client).post("/api/stage/questions", json=VALID)
    assert resp.get_json()["error"]["code"] == "SERVER_ERROR"
    assert_no_secrets(resp.get_data(as_text=True))


def test_non_stage_base_url_is_refused(app_with):
    client, adapter = real_client(base_url="https://apigw.trendyol.com")
    body = app_with(client).post("/api/stage/questions", json=VALID).get_json()
    assert body["error"]["code"] == "STAGE_ONLY" and adapter.calls == []


@pytest.mark.parametrize("field,value", [("content_id", 0), ("content_id", "12"), ("text", "  "),
                                         ("user_id", True), ("user_full_name", ""),
                                         ("show_user_name", "false"), ("channel_id", -1)])
def test_invalid_input_rejected_without_call(app_with, field, value):
    fake = FakeStageClient()
    resp = app_with(fake).post("/api/stage/questions", json={**VALID, field: value})
    assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "INVALID_REQUEST"
    assert fake.calls == []


def test_not_configured(Session):
    config = SimpleNamespace(DATABASE_URL=os.environ["TEST_DATABASE_URL"], TY_SUPPLIER_ID=None,
                             TY_API_KEY=None, TY_API_SECRET=None, TY_BASE_URL=None,
                             TY_USER_AGENT=None, POLL_INTERVAL_SECONDS=None)
    resp = create_app(config=config).test_client().post("/api/stage/questions", json=VALID)
    assert resp.status_code == 503 and resp.get_json()["error"]["code"] == "TRENDYOL_NOT_CONFIGURED"


def test_empty_error_fields_are_skipped_in_message(app_with):
    # Seen on real Stage (403): empty message/detail next to a non-empty one.
    client, _ = real_client((403, {"message": "", "detail": "", "exception": "Unexpected error",
                                   "correlationId": "c-1"}))
    body = app_with(client).post("/api/stage/questions", json=VALID).get_json()
    assert body["error"]["message"] == "Unexpected error"
    assert (body["error"]["http_status"], body["error"]["correlation_id"]) == (403, "c-1")



# --- product GET --------------------------------------------------------------

def test_get_product_by_barcode_request_and_mapping():
    adapter = FakeAdapter((200, {"barcode": "BRK-1", "contentId": 1203155798, "approved": True}))
    http = requests.Session()
    http.mount("https://", adapter)
    client = TrendyolClient(1234, KEY, SECRET, STAGE, session=http, storefront_code="TR")
    result = client.get_product_by_barcode(" BRK 1/x ")
    request, _ = adapter.calls[0]
    assert request.method == "GET"
    assert urlparse(request.url).path == "/integration/product/sellers/1234/product/BRK%201%2Fx"
    assert request.headers["storeFrontCode"] == "TR"
    assert request.headers["Authorization"] == f"Basic {BASIC}"
    assert result == {"http_status": 200, "barcode": "BRK-1", "content_id": 1203155798,
                      "product_name": None, "correlation_id": None}


def test_get_product_list_shaped_response_with_title():
    client, adapter = real_client((200, {"content": [{"barcode": "B1", "title": "Basic Tişört",
                                                      "productContentId": 42}]}))
    result = client.get_product_by_barcode("B1")
    assert (result["product_name"], result["content_id"]) == ("Basic Tişört", 42)
    assert "storeFrontCode" not in adapter.calls[0][0].headers  # not configured -> not sent


def test_product_route_success_and_connectivity(app_with):
    client, _ = real_client((200, {"barcode": "B1", "contentId": 77, "title": "Kupa"}))
    body = app_with(client).post("/api/stage/product", json={"barcode": "B1"}).get_json()
    assert body["ok"] is True
    assert (body["data"]["product_name"], body["data"]["content_id"]) == ("Kupa", 77)
    assert body["connectivity"]["product_get"]["summary"] == "SUCCESS"


def test_product_route_error_shows_status_and_message(app_with):
    client, _ = real_client((404, {"errors": [{"message": "Ürün bulunamadı"}],
                                   "correlationId": "p-9"}))
    resp = app_with(client).post("/api/stage/product", json={"barcode": "NOPE"})
    body = resp.get_json()
    assert body["error"] == {"code": "UNKNOWN_ERROR", "message": "Ürün bulunamadı",
                             "http_status": 404, "correlation_id": "p-9"}
    assert body["connectivity"]["product_get"]["summary"] == "FAILED - 404 Ürün bulunamadı"
    assert_no_secrets(resp.get_data(as_text=True))


def test_product_route_requires_barcode(app_with):
    client, adapter = real_client()
    resp = app_with(client).post("/api/stage/product", json={"barcode": "  "})
    assert resp.status_code == 400 and adapter.calls == []


# --- manual poll ------------------------------------------------------------------

class FakePollClient:
    is_stage = True

    def __init__(self, error=None):
        self.error = error

    def fetch_waiting_questions(self, page=0, size=50):
        if self.error:
            raise self.error
        item = {"trendyol_question_id": 5150, "supplier_id": 1234, "question_text": "Stage sorusu?",
                "trendyol_status": "WAITING_FOR_ANSWER", "raw_payload": {"id": 5150}}
        return {"questions": [item], "page": 0, "size": size, "total_elements": 1, "total_pages": 1}


def test_poll_route_runs_poll_and_lists_questions(app_with):
    web = app_with(FakePollClient())
    body = web.post("/api/stage/poll", json={}).get_json()
    run = body["data"]["poll_run"]
    assert (run["status"], run["fetched_count"], run["inserted_count"], run["updated_count"],
            run["error_count"]) == ("SUCCESS", 1, 1, 0, 0)
    assert [q["trendyol_question_id"] for q in body["data"]["questions"]] == [5150]
    assert body["connectivity"]["questions_get"]["summary"] == "SUCCESS"

    again = web.post("/api/stage/poll", json={}).get_json()["data"]["poll_run"]
    assert (again["inserted_count"], again["updated_count"]) == (0, 0)
    html = web.get("/stage").get_data(as_text=True)
    assert "Stage sorusu?" in html and 'data-conn="questions_get" class="conn-success"' in html


def test_poll_route_failure_is_reported(app_with):
    web = app_with(FakePollClient(error=TrendyolError(ErrorCode.AUTH_ERROR, status_code=403)))
    body = web.post("/api/stage/poll", json={}).get_json()
    assert body["ok"] is True  # the poll ran and was recorded; its own status says FAILED
    assert body["data"]["poll_run"]["status"] == "FAILED"
    assert body["data"]["poll_run"]["error_count"] == 1
    assert body["connectivity"]["questions_get"]["state"] == "FAILED"


@pytest.mark.parametrize("url,payload", [("/api/stage/product", {"barcode": "B1"}),
                                         ("/api/stage/poll", {})])
def test_all_tools_refuse_non_stage(app_with, url, payload):
    client, adapter = real_client(base_url="https://apigw.trendyol.com")
    body = app_with(client).post(url, json=payload).get_json()
    assert body["error"]["code"] == "STAGE_ONLY" and adapter.calls == []


def test_real_stage_403_body_shows_rbac_raw_error(app_with):
    # Body captured from Trendyol Stage (POST /qna/sellers/{id}/questions), 2026-09-28.
    real_body = {"timestamp": 0, "exception": "", "title": "", "status": 403,
                 "requestUri": "/sellers/2738/questions", "requestMethod": "POST",
                 "correlationId": "7c5cbc84-56b8-459a-a9a1-2cea8aac3899",
                 "errors": [{"key": "", "message": "Unexpected error", "args": None}],
                 "rawError": "RBAC: access denied"}
    client, adapter = real_client((403, real_body))
    body = app_with(client).post("/api/stage/questions", json=VALID).get_json()
    assert body["error"]["code"] == "AUTH_ERROR"
    assert body["error"]["message"].startswith("RBAC: access denied")
    assert (body["error"]["http_status"], body["error"]["correlation_id"]) == (
        403, "7c5cbc84-56b8-459a-a9a1-2cea8aac3899")
    assert body["connectivity"]["create_question_post"]["summary"].startswith(
        "FAILED - 403 RBAC: access denied")
    assert len(adapter.calls) == 1


# --- single question detail (read-only debug view) ----------------------------

DETAIL = {"id": 400082764, "text": "Bu ürün hakkında test sorusudur.", "status": "WAITING_FOR_ANSWER",
          "creationDate": 1790000000000, "productName": "Test Ürün", "productMainId": "PM-1",
          "userName": "T** K**", "showUserName": False, "webUrl": "https://www.trendyol.com/p-1",
          "imageUrl": "https://cdn.dsmcdn.com/1.jpg", "answer": None, "rejectedAnswer": None,
          "someUndocumentedField": {"nested": [1, 2]}}


class HtmlAdapter(FakeAdapter):
    """Cloudflare-style block page: HTML body, Server: cloudflare."""

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        status, text = self.replies.pop(0)
        resp = requests.Response()
        resp.status_code, resp._content = status, text.encode()
        resp.headers.update({"Content-Type": "text/html", "Server": "cloudflare"})
        resp.request, resp.url = request, request.url
        return resp


def test_detail_success_waiting_for_answer_keeps_raw_json(app_with):
    client, adapter = real_client((200, DETAIL, {"X-Correlation-Id": "c-1"}))
    resp = app_with(client).get("/api/stage/questions/400082764")
    data = resp.get_json()["data"]
    assert adapter.calls[0][0].method == "GET"
    assert adapter.calls[0][0].url.endswith("/integration/qna/sellers/1234/questions/400082764")
    assert (data["status"], data["answerable"]) == ("WAITING_FOR_ANSWER", True)
    assert data["question_id"] == 400082764 and data["correlation_id"] == "c-1"
    assert data["text"] == DETAIL["text"] and data["product_name"] == "Test Ürün"
    assert data["product_main_id"] == "PM-1" and data["user_name"] == "T** K**"
    assert data["web_url"] == DETAIL["webUrl"] and data["image_url"] == DETAIL["imageUrl"]
    assert data["creation_date"].startswith("2026-")
    assert data["raw"] == DETAIL  # full response, undocumented fields included
    assert_no_secrets(resp.get_data(as_text=True))


def test_detail_other_status_is_not_answerable_and_shows_answers(app_with):
    item = {**DETAIL, "status": "REJECTED", "answer": {"text": "Eski cevap"},
            "rejectedAnswer": {"text": "Reddedilen cevap"}}
    client, _ = real_client((200, item))
    data = app_with(client).get("/api/stage/questions/400082764").get_json()["data"]
    assert (data["status"], data["answerable"]) == ("REJECTED", False)
    assert data["answer"] == {"text": "Eski cevap"}
    assert data["rejected_answer"] == {"text": "Reddedilen cevap"}


def test_detail_never_writes_to_db(app_with, Session):
    from sqlalchemy import func, select
    from app.models import Question
    client, _ = real_client((200, DETAIL))
    app_with(client).get("/api/stage/questions/400082764")
    with Session() as session:
        assert session.scalar(select(func.count()).select_from(Question)) == 0


def test_detail_404(app_with):
    client, adapter = real_client((404, {"message": "EntityNotFoundException"},
                                    {"X-Correlation-Id": "c-404"}))
    resp = app_with(client).get("/api/stage/questions/1")
    err = resp.get_json()["error"]
    assert (resp.status_code, err["code"], err["http_status"]) == (404, "QUESTION_NOT_VISIBLE", 404)
    assert err["message"] == ("Soru oluşturuldu ancak Trendyol soru sorgulama servislerinde "
                              "henüz erişilebilir değil.")
    assert err["correlation_id"] == "c-404" and "UNKNOWN_ERROR" not in resp.get_data(as_text=True)
    assert len(adapter.calls) == 1


def test_detail_auth_error(app_with):
    client, _ = real_client((401, {"message": "Unauthorized"}))
    err = app_with(client).get("/api/stage/questions/1").get_json()["error"]
    assert (err["code"], err["http_status"]) == ("AUTH_ERROR", 401)


def test_detail_cloudflare_html_is_not_echoed(app_with):
    page = "<!DOCTYPE html><html><body>Attention Required! | Cloudflare</body></html>"
    adapter = HtmlAdapter((403, page))
    http = requests.Session()
    http.mount("https://", adapter)
    client = TrendyolClient(1234, KEY, SECRET, STAGE, session=http, sleep=lambda s: None)
    resp = app_with(client).get("/api/stage/questions/1")
    err = resp.get_json()["error"]
    assert err["code"] == "ACCESS_BLOCKED" and "<html" not in resp.get_data(as_text=True).lower()


@pytest.mark.parametrize("replies,code,http_status", [
    ([(429, {"message": "slow"})] * 3, "RATE_LIMITED", 429),
    ([(503, {"message": "down"})] * 3, "SERVER_ERROR", 503),
    ([requests.Timeout()] * 3, "NETWORK_ERROR", None),
])
def test_detail_retryable_failures(app_with, replies, code, http_status):
    client, _ = real_client(*replies)
    err = app_with(client).get("/api/stage/questions/1").get_json()["error"]
    assert (err["code"], err["http_status"]) == (code, http_status)


def test_detail_refuses_non_stage_and_bad_id(app_with):
    client, adapter = real_client(base_url="https://apigw.trendyol.com")
    web = app_with(client)
    assert web.get("/api/stage/questions/1").get_json()["error"]["code"] == "STAGE_ONLY"
    assert web.get("/api/stage/questions/0").get_json()["error"]["code"] == "INVALID_REQUEST"
    assert adapter.calls == []


def test_stage_page_has_detail_section(app_with):
    client, _ = real_client()
    html = app_with(client).get("/stage").get_data(as_text=True)
    for text in ("4. Soru Detayını Getir", "Soru Detayını Getir", "Ham Trendyol JSON",
                 'name="question_id"'):
        assert text in html


# --- test question history (debug only, separate from `questions`) -----------

LABEL = "Trendyol tarafından oluşturuldu - GET servislerinde henüz görünmüyor"


def _create(web):
    return web.post("/api/stage/questions", json={
        "content_id": 1203155798, "text": "Geçmiş sorusu", "user_id": 4147346,
        "user_full_name": "Test Kullanıcı"})


def test_create_200_appears_in_history_and_survives_reload(app_with, Session):
    client, _ = real_client((200, {"questionId": 400082765}, {"X-Correlation-Id": "c-9"}))
    web = app_with(client)
    item = _create(web).get_json()["data"]["test_question"]
    assert (item["question_id"], item["text"], item["content_id"], item["user_id"],
            item["user_full_name"], item["create_http_status"], item["correlation_id"]) == (
        400082765, "Geçmiş sorusu", 1203155798, 4147346, "Test Kullanıcı", 200, "c-9")
    assert item["created_at"] and item["label"] == LABEL and item["answerable"] is False
    html = web.get("/stage").get_data(as_text=True)  # "page refresh"
    assert "Son Oluşturulan Test Soruları" in html and "400082765" in html
    assert "Geçmiş sorusu" in html and LABEL in html
    assert web.get("/api/stage/test-questions").get_json()["data"]["test_questions"][0][
        "question_id"] == 400082765


def test_failed_create_is_not_recorded(app_with):
    client, _ = real_client((403, {"message": "RBAC: access denied"}))
    web = app_with(client)
    assert _create(web).status_code == 502
    assert web.get("/api/stage/test-questions").get_json()["data"]["test_questions"] == []


def test_history_does_not_touch_questions_table(app_with, Session):
    from sqlalchemy import func, select
    from app.models import Question
    client, _ = real_client((200, {"questionId": 400082765}))
    _create(app_with(client))
    with Session() as session:
        assert session.scalar(select(func.count()).select_from(Question)) == 0


def test_poll_returning_zero_keeps_history_and_answer_list_empty(app_with):
    class EmptyPoll(FakePollClient):
        def create_stage_question(self, **kwargs):
            return {"http_status": 200, "question_id": 400082765, "correlation_id": "c"}

        def fetch_waiting_questions(self, page=0, size=50):
            return {"questions": [], "page": 0, "size": size, "total_elements": 0,
                    "total_pages": 0}

    web = app_with(EmptyPoll())
    _create(web)
    data = web.post("/api/stage/poll", json={}).get_json()["data"]
    assert data["questions"] == []
    html = web.get("/stage").get_data(as_text=True)
    assert "400082765" in html and "Cevap bekleyen soru yok." in html
    assert "Cevapla</a>" not in html  # history rows never get an answer link


def test_waiting_questions_reach_normal_list_and_test_row_has_no_answer_button(app_with):
    class Both(FakePollClient):
        def create_stage_question(self, **kwargs):
            return {"http_status": 200, "question_id": 400082765, "correlation_id": "c"}

    web = app_with(Both())
    _create(web)
    assert [q["trendyol_question_id"] for q in
            web.post("/api/stage/poll", json={}).get_json()["data"]["questions"]] == [5150]
    index = web.get("/").get_data(as_text=True)
    assert "Stage sorusu?" in index and "Geçmiş sorusu" not in index
    assert "400082765" not in index  # not on the answer screen
    stage = web.get("/stage").get_data(as_text=True)
    assert stage.count("Cevapla</a>") == 1  # only the real WAITING question


def test_stage_page_has_moderation_note_and_label(app_with):
    client, _ = real_client()
    html = app_with(client).get("/stage").get_data(as_text=True)
    assert ("Test sorusu oluşturulduktan sonra Trendyol değerlendirme sürecinden geçer. Soru, "
            "WAITING_FOR_ANSWER durumuna geçene kadar cevap bekleyen listede görünmeyebilir.") in html
    assert "Henüz test sorusu oluşturulmadı." in html


# --- "Cevap bekleyen sorular" table ------------------------------------------------

def _seed(Session, **over):
    from app import crud
    item = {"trendyol_question_id": 7001, "supplier_id": 1234, "question_text": "Bu ürün suya dayanıklı mı?",
            "trendyol_status": "WAITING_FOR_ANSWER", "product_name": "Çanta", "product_main_id": "PM-9",
            "customer_name": "Şükrü Öztürk",
            "raw_payload": {"webUrl": "https://www.trendyol.com/x-p-1", "imageUrl": "https://cdn.dsmcdn.com/a.jpg",
                            "rejectedAnswer": {"text": "eski ret"}}}
    item.update(over)
    with Session() as s:
        crud.upsert_questions(s, [item])
        s.commit()


def _table(html):
    return html[html.index('id="stage-questions"'):html.index("</table>", html.index('id="stage-questions"'))]


def test_waiting_question_listed_with_fields_and_answer_button(app_with, Session):
    _seed(Session)
    html = app_with(FakePollClient()).get("/stage").get_data(as_text=True)
    t = _table(html)
    for expected in ("7001", "WAITING_FOR_ANSWER", "Bu ürün suya dayanıklı mı?", "Çanta", "Ana ID: PM-9",
                     "Şükrü Öztürk", 'href="https://www.trendyol.com/x-p-1"', 'src="https://cdn.dsmcdn.com/a.jpg"',
                     "rejectedAnswer", "eski ret"):
        assert expected in t
    assert 'class="btn-answer" href="/#q-1"' in t and ">Cevapla</a>" in t
    assert "<th>İşlem</th>" in html


def test_answered_question_has_no_answer_button(app_with, Session):
    _seed(Session, trendyol_status="ANSWERED")
    from app.models import Question
    with Session() as s:  # an answerable local row whose Trendyol status already moved on
        s.query(Question).update({"local_status": "NEW"})
        s.commit()
    t = _table(app_with(FakePollClient()).get("/stage").get_data(as_text=True))
    assert "btn-answer" not in t and "status-answered" in t
    assert "yerel: NEW" in t  # Trendyol status shown, local one only as secondary info


def test_empty_list_message(app_with):
    t = _table(app_with(FakePollClient()).get("/stage").get_data(as_text=True))
    assert "Cevap bekleyen soru yok." in t


def test_question_without_product_info_renders(app_with, Session):
    _seed(Session, product_name=None, product_main_id=None, customer_name=None, raw_payload=None)
    t = _table(app_with(FakePollClient()).get("/stage").get_data(as_text=True))
    assert "btn-answer" in t and "<img" not in t and "Ana ID" not in t and "<a href=\"http" not in t


def test_long_text_and_html_are_escaped(app_with, Session):
    evil = "<script>alert(1)</script><b>x</b> " + "uzun " * 600
    _seed(Session, question_text=evil, raw_payload={"webUrl": "javascript:alert(1)",
                                                    "imageUrl": "http://insecure/x.jpg"})
    t = _table(app_with(FakePollClient()).get("/stage").get_data(as_text=True))
    assert "<script>" not in t and "&lt;script&gt;alert(1)&lt;/script&gt;" in t and "<b>x</b>" not in t
    assert "uzun " * 600 in t
    assert "javascript:" not in t and "http://insecure" not in t and "<img" not in t


def test_poll_response_has_display_fields_but_no_raw_payload(app_with):
    class Rich(FakePollClient):
        def fetch_waiting_questions(self, page=0, size=50):
            r = super().fetch_waiting_questions(page, size)
            r["questions"][0]["raw_payload"] = {"id": 5150, "webUrl": "https://t.co/p", "imageUrl": "ftp://bad",
                                                "answer": {"text": "cvp"}}
            return r
    q = app_with(Rich()).post("/api/stage/poll", json={}).get_json()["data"]["questions"][0]
    assert q["web_url"] == "https://t.co/p" and q["image_url"] is None and q["answer"] == "cvp"
    assert "raw_payload" not in q


def test_stage_js_renders_table_safely(app_with):
    js = app_with(FakePollClient()).get("/static/stage.js").get_data(as_text=True)
    assert "innerHTML =" not in js and "insertAdjacentHTML" not in js and "safeHttps" in js and "Cevap bekleyen soru yok." in js
    assert 'tool === "poll") renderQuestions' in js  # list refreshes right after a successful poll
