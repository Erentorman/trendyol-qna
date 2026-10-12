"""FAZ 6: what the user sees (index + /stage), and how infrastructure errors reach them.

HTTP to Trendyol is faked; nothing here touches the real API.
"""
import re
from datetime import datetime, timezone

import pytest
import requests
from requests.adapters import BaseAdapter

from app.models import PollRun, Question
from app.trendyol_client import ErrorCode, TrendyolClient, TrendyolError
from tests.test_routes import FakeTrendyol, VALID, load, make_app, post  # noqa: F401
from tests.test_stage import app_with, real_client  # noqa: F401
from tests.test_trendyol_client import make_client

CF_PAGE = ("<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head>"
           "<body><h1>Sorry, you have been blocked</h1><p>Cloudflare Ray ID: 8f1</p></body></html>")


class RawAdapter(BaseAdapter):
    """Replays (status, raw text, headers): for non-JSON bodies such as HTML pages."""

    def __init__(self, *replies):
        super().__init__()
        self.replies, self.calls = list(replies), []

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        status, text, headers = self.replies.pop(0)
        resp = requests.Response()
        resp.status_code, resp._content = status, text.encode()
        resp.headers.update(headers)
        resp.request, resp.url = request, request.url
        return resp

    def close(self):
        pass


def raw_client(*replies, base_url="https://stageapigw.trendyol.com"):
    adapter = RawAdapter(*replies)
    http = requests.Session()
    http.mount("https://", adapter)
    return TrendyolClient(1234, "k", "s", base_url, session=http, sleep=lambda s: None), adapter


HTML = {"Content-Type": "text/html; charset=UTF-8"}
CF_HEADERS = {**HTML, "Server": "cloudflare", "CF-RAY": "8f1-IST"}


def add(Session, **fields):
    defaults = dict(trendyol_question_id=1, question_text="Pamuklu mu?", local_status="NEW",
                    trendyol_status="WAITING_FOR_ANSWER", product_name="Basic Tişört",
                    customer_name="A*** K***",
                    question_created_at=datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc))
    with Session() as s:
        q = Question(**{**defaults, **fields})
        s.add(q)
        s.commit()
        return q.id


def index(make_app):
    return make_app(FakeTrendyol()).test_client().get("/").get_data(as_text=True)


def card(html, qid):
    m = re.search(rf'<article class="card" data-question-id="{qid}".*?</article>', html, re.S)
    assert m, f"card {qid} missing"
    return m.group(0)


# --- Cloudflare / HTML error bodies never reach the user as raw HTML ---------------

def test_cloudflare_403_html_is_access_blocked_and_not_retried():
    client, adapter = raw_client((403, CF_PAGE, CF_HEADERS))
    with pytest.raises(TrendyolError) as exc:
        client.fetch_waiting_questions()
    err = exc.value
    assert (err.code, err.status_code, err.retryable) == (ErrorCode.ACCESS_BLOCKED, 403, True)
    assert len(adapter.calls) == 1  # never auto-retried: that would only prolong a block
    assert "<" not in (err.detail or "") and "<" not in err.message


def test_cloudflare_detected_from_body_alone():
    client, _ = raw_client((403, CF_PAGE, HTML))
    with pytest.raises(TrendyolError) as exc:
        client.fetch_waiting_questions()
    assert exc.value.code == ErrorCode.ACCESS_BLOCKED


def test_non_cloudflare_html_403_stays_auth_error_without_raw_html():
    client, _ = raw_client((403, "<html><body>Forbidden</body></html>", HTML))
    with pytest.raises(TrendyolError) as exc:
        client.fetch_waiting_questions()
    assert exc.value.code == ErrorCode.AUTH_ERROR
    assert "<" not in (exc.value.detail or "")


def test_json_rbac_403_is_never_a_cloudflare_block_even_with_cf_headers():
    client, adapter, _ = make_client((403, {"rawError": "RBAC: access denied"},
                                   {"Server": "cloudflare", "CF-RAY": "x"}))
    with pytest.raises(TrendyolError) as exc:
        client.fetch_waiting_questions()
    assert exc.value.code == ErrorCode.AUTH_ERROR
    assert exc.value.detail == "RBAC: access denied"


def test_html_5xx_page_is_retried_and_not_leaked():
    page = "<html><body>Bad gateway</body></html>"
    client, adapter = raw_client((502, page, HTML), (502, page, HTML), (502, page, HTML))
    with pytest.raises(TrendyolError) as exc:
        client.fetch_waiting_questions()
    assert exc.value.code == ErrorCode.SERVER_ERROR and len(adapter.calls) == 3
    assert "<" not in (exc.value.detail or "")


def test_answer_with_cloudflare_block_shows_clear_error_and_stays_retryable(Session, make_app):
    qid = add(Session)
    client, adapter = raw_client((403, CF_PAGE, CF_HEADERS), base_url="https://apigw.example")
    resp = post(make_app(client), qid)
    text = resp.get_data(as_text=True)
    assert resp.status_code == 502
    assert resp.get_json()["error"] == {
        "code": "ACCESS_BLOCKED", "message": "Trendyol blocked access (Cloudflare/IP restriction).",
        "retryable": True}
    assert "<html" not in text.lower() and "Ray ID" not in text
    q = load(Session, qid)
    assert (q.local_status, q.retryable, q.error_code) == ("FAILED", True, "ACCESS_BLOCKED")
    assert (q.answer_text, q.answer_id, q.answered_at) == (None, None, None)
    assert len(adapter.calls) == 1
    # Offered again once access is back.
    assert post(make_app(FakeTrendyol()), qid).status_code == 200


def test_stage_refresh_with_cloudflare_block_reports_failed_poll_without_html(app_with):
    client, _ = raw_client((403, CF_PAGE, CF_HEADERS))
    web = app_with(client)
    body = web.post("/api/stage/poll", json={}).get_json()
    run = body["data"]["poll_run"]
    assert run["status"] == "FAILED" and run["error_message"].startswith("ACCESS_BLOCKED")
    assert "<" not in run["error_message"]
    assert body["connectivity"]["questions_get"]["state"] == "FAILED"


def test_stage_product_cloudflare_block_message_has_no_html(app_with):
    client, _ = raw_client((403, CF_PAGE, CF_HEADERS))
    body = app_with(client).post("/api/stage/product", json={"barcode": "B1"}).get_json()
    assert body["error"]["code"] == "ACCESS_BLOCKED"
    assert "<" not in body["error"]["message"] and "Cloudflare" in body["error"]["message"]


# --- index page ----------------------------------------------------------------------

def test_empty_index_renders_cleanly(Session, make_app):
    html = index(make_app)
    assert "Cevap bekleyen soru yok." in html
    assert 'id="open-count">0<' in html
    assert "Son cevaplananlar" not in html


def test_multiple_questions_render_oldest_first(Session, make_app):
    first = add(Session, trendyol_question_id=1, question_text="Birinci soru")
    second = add(Session, trendyol_question_id=2, question_text="İkinci soru",
                 question_created_at=datetime(2026, 9, 2, tzinfo=timezone.utc))
    html = index(make_app)
    assert 'id="open-count">2<' in html
    assert html.index(f'data-question-id="{first}"') < html.index(f'data-question-id="{second}"')
    assert "Birinci soru" in html and "İkinci soru" in html


def test_turkish_and_special_characters_render_escaped(Session, make_app):
    qid = add(Session, question_text='Çok güzel ğüşiöçİı & <b>"kalın"</b> \'tırnak\' mı?',
              product_name="Çocuk Ayakkabısı <script>x</script>")
    c = card(index(make_app), qid)
    assert "Çok güzel ğüşiöçİı &amp; &lt;b&gt;" in c
    assert "<b>" not in c and "<script>" not in c and "&lt;script&gt;" in c


def test_long_question_renders_fully_and_css_wraps_it(Session, make_app):
    long_text = "Uzunsoru" * 600 + " sonu"
    qid = add(Session, question_text=long_text)
    assert long_text in card(index(make_app), qid)
    css = open("app/static/style.css", encoding="utf-8").read()
    assert re.search(r"\.question\s*\{[^}]*overflow-wrap:\s*anywhere", css)


def test_card_without_product_info_does_not_break(Session, make_app):
    qid = add(Session, product_name=None, product_main_id=None, customer_name=None,
              raw_payload=None, question_created_at=None)
    c = card(index(make_app), qid)
    assert "Ürün bilgisi yok" in c and "Müşteri" in c and "tarih yok" in c
    assert "<img" not in c and "Trendyol'da aç" not in c and "Ürün kodu" not in c
    assert "<textarea" in c


def test_card_shows_product_code_link_image_and_question_id(Session, make_app):
    qid = add(Session, trendyol_question_id=4242, product_main_id="PM-77",
              raw_payload={"id": 4242, "imageUrl": "https://cdn.dsmcdn.com/a.jpg",
                           "webUrl": "https://www.trendyol.com/x-p-1"})
    c = card(index(make_app), qid)
    assert "Soru #4242" in c and "Ürün kodu: PM-77" in c
    assert 'href="https://www.trendyol.com/x-p-1"' in c and 'rel="noopener noreferrer"' in c
    assert 'src="https://cdn.dsmcdn.com/a.jpg"' in c and 'referrerpolicy="no-referrer"' in c
    assert f'id="q-{qid}"' in c  # anchor target for the /stage "Cevapla" link


@pytest.mark.parametrize("payload", [
    {"webUrl": "javascript:alert(1)", "imageUrl": "javascript:alert(2)"},
    {"webUrl": "http://insecure.example/p", "imageUrl": "data:image/png;base64,AAAA"},
    {"webUrl": 5, "imageUrl": None},
    ["not", "a", "dict"],
])
def test_unsafe_or_odd_raw_payload_urls_are_not_rendered(Session, make_app, payload):
    qid = add(Session, raw_payload=payload)
    c = card(index(make_app), qid)
    assert "<img" not in c and "<a " not in c and "javascript:" not in c


def test_quote_in_url_cannot_break_out_of_attribute(Session, make_app):
    qid = add(Session, raw_payload={"webUrl": 'https://a.example/x" onmouseover="alert(1)'})
    c = card(index(make_app), qid)
    assert 'onmouseover="alert(1)"' not in c


def test_answered_questions_listed_without_answer_form(Session, make_app):
    open_id = add(Session, trendyol_question_id=1, question_text="Açık soru")
    add(Session, trendyol_question_id=2, question_text="Bitti soru", local_status="ANSWERED",
        trendyol_status="ANSWERED", answer_text="Evet, pamukludur.", answer_id=9001,
        answered_at=datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc))
    add(Session, trendyol_question_id=3, question_text="Kapalı soru", local_status="CLOSED")
    add(Session, trendyol_question_id=4, question_text="Kilitli soru", local_status="ANSWERING")
    html = index(make_app)
    section = html[html.index('id="answered"'):]
    assert "Bitti soru" in section and "Cevabınız:</strong> Evet, pamukludur." in section
    assert "Cevap ID: 9001" in section and "03.09.2026 09:00" in section
    assert "Kapalı soru" not in section and "Kilitli soru" not in section
    assert "<textarea" not in section and "<form" not in section  # no second answer action
    assert "Bitti soru" not in html[:html.index('id="answered"')]  # not in the open list
    assert f'data-question-id="{open_id}"' in html and 'id="open-count">1<' in html


def test_new_and_answered_are_visibly_different(Session, make_app):
    new_id = add(Session, trendyol_question_id=1)
    add(Session, trendyol_question_id=2, local_status="ANSWERED", answer_text="Cevap metni var.",
        answered_at=datetime(2026, 9, 3, tzinfo=timezone.utc))
    html = index(make_app)
    assert "status-new" in card(html, new_id)
    assert "status status-answered" in html[html.index('id="answered"'):]


# --- /stage page ---------------------------------------------------------------------

def test_stage_page_refresh_button_targets_poll_route_and_table_links_to_answering(Session, app_with):
    qid = add(Session, question_text="Stage sorusu")
    client, _ = real_client()
    web = app_with(client)
    resp = web.get("/stage")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'data-tool="poll" data-url="/api/stage/poll"' in html
    assert "<th>İşlem</th>" in html and "<th>Tarih</th>" in html and "01.09.2026 12:30" in html
    assert f'class="btn-answer" href="/#q-{qid}">Cevapla</a>' in html
    js = web.get("/static/stage.js").get_data(as_text=True)
    assert "form.dataset.url" in js and '"/#q-"' in js and "Europe/Istanbul" in js


def test_stage_page_empty_list(Session, app_with):
    client, _ = real_client()
    html = app_with(client).get("/stage").get_data(as_text=True)
    assert 'class="empty-row"' in html and 'colspan="8"' in html
    assert 'id="stage-question-count">0<' in html


def test_stage_page_escapes_question_text(Session, app_with):
    add(Session, question_text="<img src=x onerror=alert(1)> Çay & kahve")
    client, _ = real_client()
    html = app_with(client).get("/stage").get_data(as_text=True)
    assert "<img src=x" not in html and "&lt;img src=x onerror=alert(1)&gt; Çay &amp; kahve" in html
