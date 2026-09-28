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
    assert result == {"question_id": 77, "answer_id": 999, "text": "Evet, kalıbı standarttır."}


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
