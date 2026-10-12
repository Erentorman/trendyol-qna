import base64
import json
import logging
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from requests.adapters import BaseAdapter

from app.config import ConfigError
from app.trendyol_client import MAX_ATTEMPTS, TIMEOUT, ErrorCode, TrendyolClient, TrendyolError

API_KEY = "key-SECRET-123"
API_SECRET = "secret-SECRET-456"


class FakeAdapter(BaseAdapter):
    """Transport stub: records prepared requests, replays queued responses/exceptions."""

    def __init__(self, *replies):
        super().__init__()
        self.replies = list(replies)
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body, headers = (*reply, {})[:3]
        resp = requests.Response()
        resp.status_code = status
        resp._content = json.dumps(body).encode() if body is not None else b""
        resp.headers.update({"Content-Type": "application/json", **headers})
        resp.request = request
        resp.url = request.url
        return resp

    def close(self):
        pass


def make_client(*replies):
    adapter = FakeAdapter(*replies)
    session = requests.Session()
    session.mount("https://", adapter)
    sleeps = []
    client = TrendyolClient(1234, API_KEY, API_SECRET, "https://apigw.example/",
                            session=session, sleep=sleeps.append)
    return client, adapter, sleeps


PAGE = {"content": [{"id": 77, "text": "Kalıbı nasıl?", "status": "WAITING_FOR_ANSWER",
                     "userName": "A***", "productName": "Tişört", "productMainId": 555,
                     "creationDate": 1758000000000}],
        "page": 0, "size": 50, "totalElements": 1, "totalPages": 1}


def test_auth_user_agent_and_timeout_are_sent():
    client, adapter, _ = make_client((200, PAGE))
    client.fetch_waiting_questions()
    request, kwargs = adapter.calls[0]
    expected = base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert request.headers["User-Agent"] == "1234 - SelfIntegration"
    assert request.headers["Accept"] == "application/json"
    assert kwargs["timeout"] == TIMEOUT == (5, 20)


def test_fetch_waiting_questions_params_and_mapping():
    client, adapter, _ = make_client((200, PAGE))
    result = client.fetch_waiting_questions(page=2, size=10)
    url = urlparse(adapter.calls[0][0].url)
    assert adapter.calls[0][0].method == "GET"
    assert url.path == "/integration/qna/sellers/1234/questions/filter"
    params = parse_qs(url.query)
    assert params["status"] == ["WAITING_FOR_ANSWER"]
    assert params["page"] == ["2"] and params["size"] == ["10"]

    q = result["questions"][0]
    assert (q["trendyol_question_id"], q["trendyol_status"], q["product_main_id"]) == (
        77, "WAITING_FOR_ANSWER", "555")
    assert q["question_created_at"].tzinfo is not None
    assert q["raw_payload"] == PAGE["content"][0]
    assert result["total_pages"] == 1


@pytest.mark.parametrize("page,size", [(-1, 10), (0, 0), (0, 51), ("0", 10)])
def test_fetch_rejects_bad_paging_without_http(page, size):
    client, adapter, _ = make_client()
    with pytest.raises(ValueError):
        client.fetch_waiting_questions(page=page, size=size)
    assert adapter.calls == []


@pytest.mark.parametrize("text,code", [
    ("   ", ErrorCode.EMPTY_ANSWER),
    ("  kısa  ", ErrorCode.ANSWER_TOO_SHORT),
    ("x" * 2001, ErrorCode.ANSWER_TOO_LONG),
])
def test_send_answer_validates_locally(text, code):
    client, adapter, _ = make_client()
    with pytest.raises(TrendyolError) as exc:
        client.send_answer(77, text)
    assert exc.value.code == code and exc.value.status_code is None
    assert adapter.calls == []


def test_send_answer_posts_stripped_text():
    client, adapter, _ = make_client((200, {"answerId": 999}))
    result = client.send_answer(77, "  Evet, kalıbı standarttır.  ")
    request = adapter.calls[0][0]
    assert request.method == "POST"
    assert urlparse(request.url).path == "/integration/qna/sellers/1234/questions/77/answers"
    assert json.loads(request.body) == {"text": "Evet, kalıbı standarttır."}
    assert request.headers["Content-Type"] == "application/json"
    assert result == {"question_id": 77, "answer_id": 999, "text": "Evet, kalıbı standarttır.",
                      "http_status": 200, "response": {"answerId": 999}}


def test_send_answer_accepts_plain_string_success_body():
    client, _, _ = make_client((200, "Cevabınız başarıyla kaydedilmiştir."))
    assert client.send_answer(77, "Evet, stokta var.")["answer_id"] is None


def test_timeout_is_retried_then_succeeds():
    client, adapter, sleeps = make_client(requests.ReadTimeout("slow"), (200, PAGE))
    client.fetch_waiting_questions()
    assert len(adapter.calls) == 2
    assert sleeps == [1.0]


def test_5xx_retried_up_to_max_attempts():
    client, adapter, sleeps = make_client(*[(503, {"message": "down"})] * 5)
    with pytest.raises(TrendyolError) as exc:
        client.fetch_waiting_questions()
    assert len(adapter.calls) == MAX_ATTEMPTS == 3
    assert sleeps == [1.0, 2.0]  # exponential backoff, no sleep after the last attempt
    assert exc.value.to_dict() == {"code": "SERVER_ERROR", "message": exc.value.message,
                                   "retryable": True, "status_code": 503}


def test_network_error_exhausted():
    client, adapter, _ = make_client(*[requests.ConnectionError("refused")] * 3)
    with pytest.raises(TrendyolError) as exc:
        client.fetch_waiting_questions()
    assert exc.value.code == ErrorCode.NETWORK_ERROR and exc.value.retryable
    assert len(adapter.calls) == 3


def test_429_uses_retry_after_header():
    client, adapter, sleeps = make_client((429, None, {"Retry-After": "7"}), (200, PAGE))
    client.fetch_waiting_questions()
    assert sleeps == [7.0]
    assert len(adapter.calls) == 2


def test_429_without_header_uses_backoff_and_caps_huge_values():
    client, _, sleeps = make_client((429, None), (429, None, {"Retry-After": "3600"}), (200, PAGE))
    client.fetch_waiting_questions()
    assert sleeps == [1.0, 60]


def test_non_retryable_4xx_is_not_retried():
    client, adapter, sleeps = make_client((400, {"errors": [{"message": "bad"}]}), (200, PAGE))
    with pytest.raises(TrendyolError) as exc:
        client.fetch_waiting_questions()
    assert (exc.value.code, exc.value.retryable, exc.value.status_code) == (
        ErrorCode.UNKNOWN_ERROR, False, 400)
    assert len(adapter.calls) == 1 and sleeps == []


@pytest.mark.parametrize("body,code", [
    ({"errors": [{"key": "qna.answer", "message": "Soru daha önce cevaplanmış"}]},
     ErrorCode.QUESTION_ALREADY_ANSWERED),
    ({"message": "Süre limiti aşılmış"}, ErrorCode.TIME_LIMIT_EXCEEDED),
    ({"errors": [{"message": "Yasaklı kelime limiti aşılmış"}]}, ErrorCode.FORBIDDEN_WORD),
    ("Cevap çok kısa", ErrorCode.ANSWER_TOO_SHORT),
    ({"message": "Cevap çok uzun"}, ErrorCode.ANSWER_TOO_LONG),
    ({"message": "Cevap boş"}, ErrorCode.EMPTY_ANSWER),
])
def test_domain_errors_are_normalized(body, code):
    client, adapter, _ = make_client((400, body))
    with pytest.raises(TrendyolError) as exc:
        client.send_answer(77, "Geçerli uzunlukta cevap")
    assert exc.value.code == code and exc.value.retryable is False
    # The raw Trendyol text must not leak through the normalized error.
    raw = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    assert "aşılmış" not in exc.value.message and exc.value.message not in raw
    assert len(adapter.calls) == 1


def test_auth_error_not_retried():
    client, adapter, _ = make_client((403, None))
    with pytest.raises(TrendyolError) as exc:
        client.fetch_waiting_questions()
    assert exc.value.code == ErrorCode.AUTH_ERROR and len(adapter.calls) == 1


def test_secrets_do_not_leak(caplog):
    caplog.set_level(logging.DEBUG)
    basic = base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
    client, _, _ = make_client(requests.ConnectionError("refused"), (500, {"message": "x"}),
                               (418, {"message": "teapot"}))
    with pytest.raises(TrendyolError) as exc:
        client.fetch_waiting_questions()
    texts = [str(exc.value), repr(exc.value), repr(client), str(exc.value.to_dict()), caplog.text]
    for text in texts:
        for secret in (API_KEY, API_SECRET, basic):
            assert secret not in text


def test_from_config_reports_missing_names_only():
    class Cfg:
        TY_SUPPLIER_ID = "1234"
        TY_API_KEY = API_KEY
        TY_API_SECRET = None
        TY_BASE_URL = None
        TY_USER_AGENT = None

    with pytest.raises(ConfigError) as exc:
        TrendyolClient.from_config(Cfg())
    assert "TY_API_SECRET" in str(exc.value) and API_KEY not in str(exc.value)


FULL_QUESTION = {
    "id": 900, "text": "İade var mı?", "customerId": 55821, "userName": "", "showUserName": False,
    "status": "WAITING_FOR_ANSWER", "public": True, "productMainId": "PM-1",
    "productName": "Tişört", "imageUrl": "https://cdn.example/img.jpg",
    "webUrl": "https://www.trendyol.com/x-p-1", "creationDate": 1758000000000,
    "answeredDateMessage": None, "reason": None, "reportReason": None,
    "reportedDate": None, "rejectedDate": None,
    "answer": {"id": 5, "text": "Evet", "creationDate": 1758000100000, "hasPrivateInfo": False},
    "rejectedAnswer": {"id": 4, "text": "x", "creationDate": 1758000050000, "reason": "r"},
    "someFutureField": {"nested": [1, 2]},  # undocumented fields must survive too
}


def test_map_question_keeps_full_raw_payload():
    page = {**PAGE, "content": [FULL_QUESTION]}
    client, _, _ = make_client((200, page))
    q = client.fetch_waiting_questions()["questions"][0]
    assert q["raw_payload"] == FULL_QUESTION


def _params(adapter, call=0):
    return parse_qs(urlparse(adapter.calls[call][0].url).query)


def test_default_request_has_no_optional_filters():
    client, adapter, _ = make_client((200, PAGE))
    client.fetch_waiting_questions()
    params = _params(adapter)
    assert params["status"] == ["WAITING_FOR_ANSWER"]
    assert params["orderByField"] == ["CreatedDate"] and params["orderByDirection"] == ["ASC"]
    assert params["supplierId"] == ["1234"] and params["page"] == ["0"] and params["size"] == ["50"]
    assert not {"barcode", "startDate", "endDate"} & set(params)


def test_barcode_filter_is_forwarded():
    client, adapter, _ = make_client((200, PAGE))
    client.fetch_waiting_questions(barcode=" ABC-1 ")
    params = _params(adapter)
    assert params["barcode"] == ["ABC-1"] and params["status"] == ["WAITING_FOR_ANSWER"]


def test_start_and_end_date_are_forwarded_as_epoch_millis():
    client, adapter, _ = make_client((200, PAGE))
    client.fetch_waiting_questions(start_date=1758000000000, end_date=1758600000000)
    params = _params(adapter)
    assert params["startDate"] == ["1758000000000"] and params["endDate"] == ["1758600000000"]


def test_order_by_is_forwarded():
    client, adapter, _ = make_client((200, PAGE))
    client.fetch_waiting_questions(order_by_field="LastModifiedDate", order_by_direction="DESC")
    params = _params(adapter)
    assert params["orderByField"] == ["LastModifiedDate"] and params["orderByDirection"] == ["DESC"]


@pytest.mark.parametrize("kwargs", [
    {"order_by_field": "Name"}, {"order_by_direction": "UP"}, {"barcode": "  "},
    {"barcode": 5}, {"start_date": "1"}, {"start_date": True}, {"end_date": -1},
    {"start_date": 20, "end_date": 10},
])
def test_invalid_filters_rejected_without_http(kwargs):
    client, adapter, _ = make_client()
    with pytest.raises(ValueError):
        client.fetch_waiting_questions(**kwargs)
    assert adapter.calls == []


# --- GET /questions/{id} (on-demand detail; not used by the poll) ---------------

def test_get_question_request_and_mapping():
    client, adapter, _ = make_client((200, FULL_QUESTION, {"X-Correlation-Id": "c-1"}))
    result = client.get_question(900)
    request = adapter.calls[0][0]
    url = urlparse(request.url)
    assert request.method == "GET" and url.path == "/integration/qna/sellers/1234/questions/900"
    assert url.query == "" and request.body is None
    assert result["http_status"] == 200 and result["correlation_id"] == "c-1"
    q = result["question"]
    assert (q["trendyol_question_id"], q["product_main_id"]) == (900, "PM-1")
    assert q["raw_payload"] == FULL_QUESTION  # full object, unknown fields included


@pytest.mark.parametrize("bad", [0, -1, "5", True, None])
def test_get_question_rejects_bad_id_without_http(bad):
    client, adapter, _ = make_client()
    with pytest.raises(ValueError):
        client.get_question(bad)
    assert adapter.calls == []


@pytest.mark.parametrize("status,code,retried", [
    (404, ErrorCode.UNKNOWN_ERROR, False),  # no dedicated "not found" code; status_code tells
    (401, ErrorCode.AUTH_ERROR, False),
    (403, ErrorCode.AUTH_ERROR, False),
])
def test_get_question_client_errors_are_normalized_and_not_retried(status, code, retried):
    client, adapter, sleeps = make_client((status, {"message": "x"}))
    with pytest.raises(TrendyolError) as exc:
        client.get_question(900)
    assert (exc.value.code, exc.value.status_code, exc.value.retryable) == (code, status, False)
    assert len(adapter.calls) == 1 and sleeps == []


def test_get_question_429_uses_retry_after_then_succeeds():
    client, adapter, sleeps = make_client((429, {"message": "slow"}, {"Retry-After": "3"}),
                                          (200, FULL_QUESTION))
    assert client.get_question(900)["http_status"] == 200
    assert len(adapter.calls) == 2 and sleeps == [3.0]


def test_get_question_5xx_retried_then_raises():
    client, adapter, _ = make_client(*[(503, {"message": "down"})] * MAX_ATTEMPTS)
    with pytest.raises(TrendyolError) as exc:
        client.get_question(900)
    assert exc.value.code == ErrorCode.SERVER_ERROR and len(adapter.calls) == MAX_ATTEMPTS


def test_get_question_timeout_retried_then_raises():
    client, adapter, _ = make_client(*[requests.Timeout()] * MAX_ATTEMPTS)
    with pytest.raises(TrendyolError) as exc:
        client.get_question(900)
    assert exc.value.code == ErrorCode.NETWORK_ERROR and len(adapter.calls) == MAX_ATTEMPTS


@pytest.mark.parametrize("body", [None, ["not", "an", "object"], {"no": "id"}, "text"])
def test_get_question_invalid_200_body_is_an_error_not_a_question(body):
    client, adapter, _ = make_client((200, body))
    with pytest.raises(TrendyolError) as exc:
        client.get_question(900)
    assert exc.value.code == ErrorCode.UNKNOWN_ERROR and exc.value.status_code == 200
    assert len(adapter.calls) == 1


def test_merge_question_payload_loses_nothing():
    from app.trendyol_client import merge_question_payload
    listed = {"id": 1, "text": "a", "listOnly": 1, "imageUrl": "img", "answer": None}
    detail = {"id": 1, "text": "b", "detailOnly": 2, "imageUrl": None,
              "answer": {"text": "cevap"}}
    merged = merge_question_payload(listed, detail)
    assert merged == {"id": 1, "text": "b", "listOnly": 1, "detailOnly": 2,
                      "imageUrl": "img", "answer": {"text": "cevap"}}
    assert listed["imageUrl"] == "img" and "detailOnly" not in listed  # inputs untouched
    assert merge_question_payload(listed, None) == listed


# --- POST /questions/{id}/answers (FAZ 5) ----------------------------------------

OK_TEXT = "Evet, stokta var."


def _answer_path(adapter):
    return urlparse(adapter.calls[0][0].url).path


@pytest.mark.parametrize("length", [10, 11, 1999, 2000])
def test_send_answer_accepts_documented_length_boundaries(length):
    client, adapter, _ = make_client((200, {"answerId": 1}))
    assert client.send_answer(77, "x" * length)["answer_id"] == 1
    assert json.loads(adapter.calls[0][0].body) == {"text": "x" * length}


@pytest.mark.parametrize("text,code", [
    (None, ErrorCode.EMPTY_ANSWER), ("", ErrorCode.EMPTY_ANSWER),
    ("\n\t  ", ErrorCode.EMPTY_ANSWER), ("x" * 9, ErrorCode.ANSWER_TOO_SHORT),
    ("  " + "x" * 9 + "  ", ErrorCode.ANSWER_TOO_SHORT),  # whitespace does not count
    ("x" * 2001, ErrorCode.ANSWER_TOO_LONG),
])
def test_send_answer_rejects_invalid_text_before_http(text, code):
    client, adapter, _ = make_client()
    with pytest.raises(TrendyolError) as exc:
        client.send_answer(77, text)
    assert exc.value.code == code and adapter.calls == []


@pytest.mark.parametrize("bad", [0, -5, "77", 7.0, True, None])
def test_send_answer_rejects_invalid_question_id_before_http(bad):
    client, adapter, _ = make_client()
    with pytest.raises(ValueError):
        client.send_answer(bad, OK_TEXT)
    assert adapter.calls == []


def test_send_answer_uses_trendyol_id_in_path_and_returns_full_response():
    body = {"answerId": 42, "extra": {"kept": True}}
    client, adapter, _ = make_client((200, body))
    result = client.send_answer(900, OK_TEXT)
    assert _answer_path(adapter) == "/integration/qna/sellers/1234/questions/900/answers"
    assert (result["answer_id"], result["http_status"], result["response"]) == (42, 200, body)


@pytest.mark.parametrize("body,expected", [
    ({"answerId": 0}, 0),               # documented sample value
    ({"answerId": "15"}, 15),           # numeric string tolerated
    ({"answerId": True}, None), ({"answerId": "abc"}, None), ({"answerId": None}, None),
    ({"message": "ok"}, None),          # successful but no answerId
    ("Cevabınız başarıyla kaydedilmiştir.", None),
    (["x"], None),
])
def test_send_answer_answer_id_variants(body, expected):
    client, _, _ = make_client((200, body))
    result = client.send_answer(77, OK_TEXT)
    assert result["answer_id"] == expected and result["response"] == body


def test_send_answer_malformed_2xx_json_is_still_success_without_answer_id():
    # Not retried and not turned into an error: Trendyol accepted it, re-sending would
    # only produce "already answered".
    adapter = FakeAdapter((200, {"answerId": 1}))
    session = requests.Session()
    session.mount("https://", adapter)
    client = TrendyolClient(1234, API_KEY, API_SECRET, "https://apigw.example/", session=session,
                            sleep=lambda s: None)
    original = adapter.send
    adapter.send = lambda req, **kw: _with_body(original(req, **kw), b"{not json")
    result = client.send_answer(77, OK_TEXT)
    assert (result["answer_id"], result["response"], result["http_status"]) == (None, None, 200)
    assert len(adapter.calls) == 1


def _with_body(resp, content):
    resp._content = content
    return resp


@pytest.mark.parametrize("status,code", [(401, ErrorCode.AUTH_ERROR), (403, ErrorCode.AUTH_ERROR),
                                         (404, ErrorCode.UNKNOWN_ERROR)])
def test_send_answer_auth_and_not_found_not_retried(status, code):
    client, adapter, sleeps = make_client((status, {"message": "nope"}))
    with pytest.raises(TrendyolError) as exc:
        client.send_answer(77, OK_TEXT)
    assert (exc.value.code, exc.value.status_code, exc.value.retryable) == (code, status, False)
    assert len(adapter.calls) == 1 and sleeps == []


def test_send_answer_429_waits_retry_after_then_succeeds():
    client, adapter, sleeps = make_client((429, None, {"Retry-After": "2"}), (200, {"answerId": 5}))
    assert client.send_answer(77, OK_TEXT)["answer_id"] == 5
    assert sleeps == [2.0] and len(adapter.calls) == 2


def test_send_answer_429_exhausted_is_retryable_error():
    client, adapter, _ = make_client(*[(429, None)] * MAX_ATTEMPTS)
    with pytest.raises(TrendyolError) as exc:
        client.send_answer(77, OK_TEXT)
    assert (exc.value.code, exc.value.retryable) == (ErrorCode.RATE_LIMITED, True)
    assert len(adapter.calls) == MAX_ATTEMPTS


def test_send_answer_5xx_exhausted_is_retryable_error():
    client, adapter, _ = make_client(*[(502, {"message": "bad gw"})] * MAX_ATTEMPTS)
    with pytest.raises(TrendyolError) as exc:
        client.send_answer(77, OK_TEXT)
    assert (exc.value.code, exc.value.retryable) == (ErrorCode.SERVER_ERROR, True)
    assert len(adapter.calls) == MAX_ATTEMPTS


def test_send_answer_timeout_then_success_posts_same_body_twice():
    client, adapter, _ = make_client(requests.ReadTimeout("slow"), (200, {"answerId": 8}))
    assert client.send_answer(77, OK_TEXT)["answer_id"] == 8
    bodies = [json.loads(call[0].body) for call in adapter.calls]
    assert bodies == [{"text": OK_TEXT}] * 2


def test_send_answer_timeout_exhausted_is_retryable_network_error():
    client, adapter, _ = make_client(*[requests.ConnectTimeout("t")] * MAX_ATTEMPTS)
    with pytest.raises(TrendyolError) as exc:
        client.send_answer(77, OK_TEXT)
    assert (exc.value.code, exc.value.retryable) == (ErrorCode.NETWORK_ERROR, True)


@pytest.mark.parametrize("message,code", [
    # Exact texts from the official "Müşteri Sorularını Cevaplama" guide.
    ("Bu soru daha önce cevaplandı. Cevaplanmış sorular tekrar cevaplanamaz.",
     ErrorCode.QUESTION_ALREADY_ANSWERED),
    ("Belirtilen süre içinde cevap vermediğiniz için soru kapatılmıştır.",
     ErrorCode.TIME_LIMIT_EXCEEDED),
    ("Cevabınızda yasaklı kelime kullanma limitini aştığınız için soru kapatılmıştır.",
     ErrorCode.FORBIDDEN_WORD),
    ("Cevabınız 10 karakterden uzun olmalıdır.", ErrorCode.ANSWER_TOO_SHORT),
    ("Cevabınız 2000 karakterden uzun olamaz.", ErrorCode.ANSWER_TOO_LONG),
    ("Bir cevap giriniz.", ErrorCode.EMPTY_ANSWER),
    # English guide versions.
    ("This question has already been answered. Answered questions cannot be re-answered.",
     ErrorCode.QUESTION_ALREADY_ANSWERED),
    ("The question has been closed because you did not respond within the allocated time.",
     ErrorCode.TIME_LIMIT_EXCEEDED),
    ("The question has been closed because you exceeded the limit for using forbidden words "
     "in your response.", ErrorCode.FORBIDDEN_WORD),
    ("Your response must be at least 10 characters long.", ErrorCode.ANSWER_TOO_SHORT),
    ("Your response cannot exceed 2000 characters.", ErrorCode.ANSWER_TOO_LONG),
    ("Please enter a response.", ErrorCode.EMPTY_ANSWER),
])
def test_documented_answer_error_messages_map_to_domain_codes(message, code):
    client, adapter, _ = make_client((400, {"message": message}))
    with pytest.raises(TrendyolError) as exc:
        client.send_answer(77, OK_TEXT)
    assert exc.value.code == code and exc.value.retryable is False
    assert len(adapter.calls) == 1
