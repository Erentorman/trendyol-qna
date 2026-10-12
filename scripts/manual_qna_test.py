"""Trendyol STAGE Soru-Cevap manuel testi (bağımsız script).

Akış: cevap bekleyen (UNANSWERED) soruyu çek -> göster -> cevap al -> onay al -> gönder.

Gerekli ortam değişkenleri (.env dosyasından okunur, koda yazılmaz):
  TY_SUPPLIER_ID, TY_API_KEY, TY_API_SECRET, TY_BASE_URL (https://stageapigw.trendyol.com)
İsteğe bağlı: TY_USER_AGENT

Çalıştırma:  python scripts/manual_qna_test.py
Uygulamanın (Flask, veritabanı, scheduler) hiçbir parçasına bağlı değildir.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

STAGE_HOST = "stageapigw.trendyol.com"
STATUS = "UNANSWERED"  # Stage'de cevap bekleyen sorunun gerçek statüsü
LOOKBACK_DAYS = 14
PAGE_SIZE = 50
MIN_ANSWER = 10
MAX_ANSWER = 2000
TIMEOUT = (5, 20)


def load_settings(env=None):
    """Ayarları ortamdan oku; Stage dışındaki adresleri reddet."""
    env = os.environ if env is None else env
    missing = [k for k in ("TY_SUPPLIER_ID", "TY_API_KEY", "TY_API_SECRET", "TY_BASE_URL")
               if not (env.get(k) or "").strip()]
    if missing:
        raise SystemExit(f"Eksik ayar(lar): {', '.join(missing)} (.env dosyasını kontrol edin)")
    base = env["TY_BASE_URL"].strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or parsed.hostname != STAGE_HOST:
        raise SystemExit(f"REDDEDİLDİ: bu script yalnızca https://{STAGE_HOST} ile çalışır. "
                         "Production'a istek atılmaz.")
    seller = env["TY_SUPPLIER_ID"].strip()
    if not seller.isdigit():
        raise SystemExit("TY_SUPPLIER_ID sayı olmalıdır.")
    return {
        "seller_id": seller,
        "base_url": base,
        "auth": (env["TY_API_KEY"].strip(), env["TY_API_SECRET"].strip()),
        "user_agent": (env.get("TY_USER_AGENT") or "").strip() or f"{seller} - SelfIntegration",
    }


def date_window(now=None, days=LOOKBACK_DAYS):
    """(startDate, endDate) epoch milisaniye; son `days` günü kapsar."""
    now = now or datetime.now(timezone.utc)
    # Trendyol aralığı en fazla iki hafta ile sınırlar: sınırın hemen altında kal.
    start = now - timedelta(days=days) + timedelta(minutes=1)
    return int(start.timestamp() * 1000), int(now.timestamp() * 1000)


def correlation_id(resp):
    for header in ("X-Correlation-Id", "X-CorrelationId", "CorrelationId"):
        if resp.headers.get(header):
            return resp.headers[header]
    body = parse_json(resp)
    if isinstance(body, dict):
        return body.get("correlationId") or body.get("correlation_id")
    return None


def parse_json(resp):
    try:
        return resp.json()
    except ValueError:
        return None


def error_detail(resp):
    """Trendyol hata gövdesinden okunur bir metin (HTML sayfası asla basılmaz)."""
    body = parse_json(resp)
    if isinstance(body, dict):
        parts = [str(body[k]) for k in ("message", "detail", "rawError", "title", "exception")
                 if isinstance(body.get(k), str) and body[k].strip()]
        for err in body.get("errors") or []:
            if isinstance(err, dict):
                parts += [str(err[k]) for k in ("key", "message") if err.get(k)]
        text = " | ".join(parts)
    elif isinstance(body, str):
        text = body
    else:
        text = resp.text or ""
        if text.lstrip().lower().startswith(("<!doctype", "<html")):
            text = "HTML hata sayfası (JSON değil)"
    return text.strip()[:500] or "-"


def fetch_unanswered(s):
    """Cevap bekleyen soruları getir. Dönüş: (HTTP status, içerik listesi)."""
    start, end = date_window()
    params = {
        "supplierId": s["seller_id"],
        "status": STATUS,
        "page": 0,
        "size": PAGE_SIZE,
        "orderByField": "CreatedDate",
        "orderByDirection": "DESC",
        "startDate": start,
        "endDate": end,
    }
    url = f"{s['base_url']}/integration/qna/sellers/{s['seller_id']}/questions/filter"
    resp = requests.get(url, params=params, auth=s["auth"], timeout=TIMEOUT,
                        headers={"User-Agent": s["user_agent"], "Accept": "application/json"})
    if not resp.ok:
        raise ApiError(resp)
    body = parse_json(resp)
    return resp.status_code, (body.get("content") or []) if isinstance(body, dict) else []


def post_answer(s, question_id, text):
    """Cevabı gönder. Dönüş: (HTTP status, answerId veya None)."""
    url = f"{s['base_url']}/integration/qna/sellers/{s['seller_id']}/questions/{question_id}/answers"
    resp = requests.post(url, json={"text": text}, auth=s["auth"], timeout=TIMEOUT,
                         headers={"User-Agent": s["user_agent"], "Accept": "application/json"})
    if not resp.ok:
        raise ApiError(resp)
    body = parse_json(resp)
    answer_id = body.get("answerId") if isinstance(body, dict) else None
    return resp.status_code, answer_id


class ApiError(Exception):
    def __init__(self, resp):
        self.status = resp.status_code
        self.detail = error_detail(resp)
        self.correlation_id = correlation_id(resp)
        super().__init__(f"HTTP {self.status}")


def has_answer(question):
    """True ise soru zaten cevaplanmış: `answer` içinde id veya cevap metni var.

    answer yok / null / {} / anlamlı veri içermeyen nesne -> cevapsız sayılır.
    """
    answer = question.get("answer")
    if not isinstance(answer, dict):
        return False
    text = answer.get("text")
    return answer.get("id") is not None or (isinstance(text, str) and bool(text.strip()))


def pick_unanswered(questions):
    """(ilk gerçekten cevapsız soru veya None, atlanan sorular listesi)."""
    skipped = []
    for question in questions:
        if has_answer(question):
            skipped.append(question)
        else:
            return question, skipped
    return None, skipped


def format_date(millis):
    if not isinstance(millis, (int, float)):
        return "-"
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).astimezone().strftime(
        "%d.%m.%Y %H:%M")


def show_question(q):
    print("\nTRENDYOL STAGE QNA\n")
    print(f"Question ID: {q.get('id')}")
    print(f"Status: {q.get('status')}")
    print(f"Soru: {q.get('text')}")
    print(f"Kullanıcı: {q.get('userName') or '-'}")
    print(f"Ürün: {q.get('productName') or '-'}")
    print(f"Product Main ID: {q.get('productMainId') or '-'}")
    print(f"Created Date: {format_date(q.get('creationDate'))}")
    print(f"Web URL: {q.get('webUrl') or '-'}")
    print("\nRaw JSON:")
    print(json.dumps(q, ensure_ascii=False, indent=2))


def print_failure(exc):
    print("\nCEVAP BAŞARISIZ")
    print(f"HTTP Status: {exc.status}")
    print(f"Error: HTTP {exc.status}")
    print(f"Detail: {exc.detail}")
    print(f"Correlation ID: {exc.correlation_id or '-'}")


def run(s, input_fn=input):
    print("Trendyol Stage Manuel QnA Testi")
    try:
        http_status, questions = fetch_unanswered(s)
    except ApiError as exc:
        print("\nSORULAR ALINAMADI")
        print(f"HTTP Status: {exc.status}\nDetail: {exc.detail}\n"
              f"Correlation ID: {exc.correlation_id or '-'}")
        return 1
    except requests.RequestException as exc:
        print(f"\nSORULAR ALINAMADI: bağlantı hatası ({type(exc).__name__})")
        return 1

    print(f"GET başarılı (HTTP {http_status}); {STATUS} soru sayısı: {len(questions)}")
    if not questions:
        print(f"\n{STATUS} durumunda cevap bekleyen soru bulunamadı.")
        return 0

    question, skipped = pick_unanswered(questions)
    for item in skipped:
        print(f"Atlandı: Question ID {item.get('id')} zaten cevaplanmış.")
    if question is None:
        print(f"\n{STATUS} filtresinde kayıt bulundu ancak gerçekten cevapsız soru bulunamadı.")
        return 0
    show_question(question)

    answer = input_fn("\nCevap: ").strip()
    if len(answer) < MIN_ANSWER:
        print(f"Cevap en az {MIN_ANSWER} karakter olmalı. API'ye gönderilmedi.")
        return 1
    if len(answer) > MAX_ANSWER:
        print(f"Cevap en fazla {MAX_ANSWER} karakter olabilir. API'ye gönderilmedi.")
        return 1

    if input_fn("Bu cevabı göndermek istiyor musunuz? [y/N] ").strip() not in ("y", "Y"):
        print("Cevap gönderilmedi.")
        return 0

    try:
        http_status, answer_id = post_answer(s, question["id"], answer)
    except ApiError as exc:
        print_failure(exc)
        return 1
    except requests.RequestException as exc:
        print(f"\nCEVAP BAŞARISIZ\nError: bağlantı hatası ({type(exc).__name__})")
        return 1

    print("\nCEVAP BAŞARILI")
    print(f"Question ID: {question['id']}")
    print(f"Answer ID: {answer_id if answer_id is not None else '-'}")
    print(f"HTTP Status: {http_status}")
    return 0


def main():
    load_dotenv()
    return run(load_settings())


if __name__ == "__main__":
    sys.exit(main())
