"""Single entry point for all Trendyol QnA HTTP traffic.

Nothing else in the app should import `requests` for Trendyol: auth, headers,
timeouts, retries and error normalization live here only.

API contract (developers.trendyol.com, Soru-Cevap entegrasyonu):
  GET  {base}/integration/qna/sellers/{sellerId}/questions/filter
  POST {base}/integration/qna/sellers/{sellerId}/questions/{id}/answers  {"text": ...}
"""

import email.utils
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests

from app.config import ConfigError

logger = logging.getLogger(__name__)

TIMEOUT = (5, 20)  # (connect, read) seconds
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0  # 1s, 2s, 4s ...
MAX_RETRY_AFTER_SECONDS = 60  # never let a server header stall a worker for longer
MAX_PAGE_SIZE = 50  # documented max for questions/filter
ANSWER_MIN_LENGTH = 10
ANSWER_MAX_LENGTH = 2000
STAGE_HOST = "stageapigw.trendyol.com"
DETAIL_MAX_LENGTH = 300
ORDER_BY_FIELDS = ("CreatedDate", "LastModifiedDate")  # documented values
ORDER_BY_DIRECTIONS = ("ASC", "DESC")


class ErrorCode:
    QUESTION_ALREADY_ANSWERED = "QUESTION_ALREADY_ANSWERED"
    TIME_LIMIT_EXCEEDED = "TIME_LIMIT_EXCEEDED"
    FORBIDDEN_WORD = "FORBIDDEN_WORD"
    ANSWER_TOO_SHORT = "ANSWER_TOO_SHORT"
    ANSWER_TOO_LONG = "ANSWER_TOO_LONG"
    EMPTY_ANSWER = "EMPTY_ANSWER"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


# Stable, safe messages: callers/UI get these, never Trendyol's raw body.
MESSAGES = {
    ErrorCode.QUESTION_ALREADY_ANSWERED: "Question has already been answered.",
    ErrorCode.TIME_LIMIT_EXCEEDED: "Answer time limit for this question has expired.",
    ErrorCode.FORBIDDEN_WORD: "Answer contains forbidden words.",
    ErrorCode.ANSWER_TOO_SHORT: f"Answer must be at least {ANSWER_MIN_LENGTH} characters.",
    ErrorCode.ANSWER_TOO_LONG: f"Answer must be at most {ANSWER_MAX_LENGTH} characters.",
    ErrorCode.EMPTY_ANSWER: "Answer is empty.",
    ErrorCode.RATE_LIMITED: "Trendyol rate limit reached.",
    ErrorCode.SERVER_ERROR: "Trendyol server error.",
    ErrorCode.NETWORK_ERROR: "Could not reach Trendyol (timeout or connection error).",
    ErrorCode.AUTH_ERROR: "Trendyol rejected the credentials or User-Agent.",
    # Infrastructure block (Cloudflare / IP), not a code or credential problem.
    ErrorCode.ACCESS_BLOCKED: "Trendyol blocked access (Cloudflare/IP restriction).",
    ErrorCode.UNKNOWN_ERROR: "Unexpected Trendyol error.",
}

# Substrings of Trendyol's (Turkish) error messages -> domain code. Order matters.
DOMAIN_PATTERNS = (
    (("daha önce cevaplan", "already answered", "already been answered"),
     ErrorCode.QUESTION_ALREADY_ANSWERED),
    (("süre limit", "time limit", "süre içinde", "allocated time"), ErrorCode.TIME_LIMIT_EXCEEDED),
    (("yasaklı", "forbidden"), ErrorCode.FORBIDDEN_WORD),
    (("çok kısa", "too short", "karakterden uzun olmalı", "at least 10 characters"),
     ErrorCode.ANSWER_TOO_SHORT),
    (("çok uzun", "too long", "karakterden uzun olamaz", "cannot exceed"),
     ErrorCode.ANSWER_TOO_LONG),
    (("cevap boş", "empty", "cevap giriniz", "enter a response"), ErrorCode.EMPTY_ANSWER),
)
# The Turkish/English texts above include the ones published in the official
# "Müşteri Sorularını Cevaplama" guide (developers.trendyol.com), e.g. "Belirtilen süre
# içinde cevap vermediğiniz için soru kapatılmıştır." and "Bir cevap giriniz.".


class TrendyolError(Exception):
    """Normalized Trendyol failure. The only error type this module raises for API calls."""

    def __init__(self, code, retryable=False, status_code=None, detail=None,
                 correlation_id=None):
        self.code = code
        self.message = MESSAGES[code]
        self.retryable = retryable
        self.status_code = status_code
        # Trendyol's own error text (truncated). NOT part of to_dict(): the answer flow
        # never shows it; only the Stage test screen does, on purpose.
        self.detail = detail
        self.correlation_id = correlation_id
        super().__init__(f"{code}: {self.message}")

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "status_code": self.status_code,
        }


def _looks_like_html(resp):
    head = (resp.text or "").lstrip()[:200].lower()
    return "html" in resp.headers.get("Content-Type", "").lower() or head.startswith(
        ("<!doctype", "<html", "<head", "<body"))


def _is_cloudflare_block(resp):
    """403 with a non-JSON body that comes from Cloudflare (block / challenge page).

    A JSON 403 (e.g. the gateway's RBAC answer) is never treated as a block, even if
    Cloudflare headers are present on every response.
    """
    if resp.status_code != 403 or _json_or_none(resp) is not None:
        return False
    server = resp.headers.get("Server", "").lower()
    return ("cloudflare" in server or bool(resp.headers.get("CF-RAY"))
            or "cloudflare" in (resp.text or "").lower())


def _error_text(resp):
    """Collect human-readable message strings from an error body, whatever its shape."""
    try:
        body = resp.json()
    except ValueError:
        if _looks_like_html(resp):
            return "HTML error page (not JSON)"  # never pass a raw HTML page on
        return resp.text or ""
    parts = []
    if isinstance(body, str):
        parts.append(body)
    elif isinstance(body, dict):
        # rawError carries the gateway reason, e.g. "RBAC: access denied" (seen on Stage 403).
        for key in ("rawError", "message", "detail", "title", "exception"):
            if isinstance(body.get(key), str):
                parts.append(body[key])
        for err in body.get("errors") or []:
            if isinstance(err, dict):
                parts += [str(err[k]) for k in ("key", "message") if err.get(k)]
    return " | ".join(p.strip() for p in parts if p and p.strip())  # skip empty fields


def _json_or_none(resp):
    try:
        return resp.json()
    except ValueError:
        return None


def _correlation_id(resp):
    for header in ("X-Correlation-Id", "X-CorrelationId", "CorrelationId"):
        if resp.headers.get(header):
            return resp.headers[header]
    body = _json_or_none(resp)
    if isinstance(body, dict):
        return body.get("correlationId") or body.get("correlation_id")
    return None


def normalize_error(resp):
    status = resp.status_code
    text = _error_text(resp)
    extra = {"status_code": status, "detail": text[:DETAIL_MAX_LENGTH] or None,
             "correlation_id": _correlation_id(resp)}
    if _is_cloudflare_block(resp):
        # Retryable for the caller (the block is temporary) but not auto-retried in
        # _request: hammering a blocked endpoint would only prolong the block.
        extra["detail"] = "Cloudflare/IP block page (HTML response)"
        return TrendyolError(ErrorCode.ACCESS_BLOCKED, retryable=True, **extra)
    if status == 429:
        return TrendyolError(ErrorCode.RATE_LIMITED, retryable=True, **extra)
    if status >= 500:
        return TrendyolError(ErrorCode.SERVER_ERROR, retryable=True, **extra)

    lowered = text.lower()
    for needles, code in DOMAIN_PATTERNS:
        if any(n in lowered for n in needles):
            return TrendyolError(code, **extra)

    if status in (401, 403):
        return TrendyolError(ErrorCode.AUTH_ERROR, **extra)
    # Keep the original text in server logs only, truncated, for debugging.
    logger.warning("Unmapped Trendyol error status=%s body=%.200s", status, text)
    return TrendyolError(ErrorCode.UNKNOWN_ERROR, **extra)


def _retry_after_seconds(resp):
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:  # HTTP-date form
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def validate_answer_text(text):
    """Strip and check Trendyol's answer rules locally. Returns the stripped text."""
    text = (text or "").strip()
    if not text:
        raise TrendyolError(ErrorCode.EMPTY_ANSWER)
    if len(text) < ANSWER_MIN_LENGTH:
        raise TrendyolError(ErrorCode.ANSWER_TOO_SHORT)
    if len(text) > ANSWER_MAX_LENGTH:
        raise TrendyolError(ErrorCode.ANSWER_TOO_LONG)
    return text


def _answer_id(body):
    """`answerId` from a success body as an int, else None (never raises, never a bool)."""
    value = body.get("answerId") if isinstance(body, dict) else None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _from_millis(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


def merge_question_payload(list_item, detail_item):
    """Combine a list item and a detail item into one raw_payload without losing data.

    Detail wins for keys it has, except that a null in the detail never erases a value
    the list already had. Keys present in only one of them are kept.
    """
    merged = dict(list_item or {})
    for key, value in (detail_item or {}).items():
        if value is not None or key not in merged:
            merged[key] = value
    return merged


class TrendyolClient:
    def __init__(self, supplier_id, api_key, api_secret, base_url, user_agent=None,
                 session=None, sleep=time.sleep, storefront_code=None):
        self.supplier_id = int(supplier_id)
        self.storefront_code = storefront_code
        self.base_url = base_url.rstrip("/")
        self._sleep = sleep  # injectable so tests don't actually wait
        self._session = session or requests.Session()
        self._session.auth = (api_key, api_secret)  # HTTP Basic; never logged
        self.user_agent = user_agent or f"{self.supplier_id} - SelfIntegration"  # not a secret
        self._session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        })

    @classmethod
    def from_config(cls, config, **kwargs):
        required = ("TY_SUPPLIER_ID", "TY_API_KEY", "TY_API_SECRET", "TY_BASE_URL")
        missing = [name for name in required if not getattr(config, name)]
        if missing:
            raise ConfigError(f"Missing Trendyol settings: {', '.join(missing)}")
        kwargs.setdefault("storefront_code", getattr(config, "TY_STOREFRONT_CODE", None))
        return cls(config.TY_SUPPLIER_ID, config.TY_API_KEY, config.TY_API_SECRET,
                   config.TY_BASE_URL, config.TY_USER_AGENT, **kwargs)

    def __repr__(self):
        return f"TrendyolClient(supplier_id={self.supplier_id}, base_url={self.base_url!r})"

    @property
    def _qna_path(self):
        return f"/integration/qna/sellers/{self.supplier_id}/questions"

    @property
    def is_stage(self):
        return urlparse(self.base_url).hostname == STAGE_HOST

    def _request(self, method, path, *, params=None, json=None, headers=None,
                 max_attempts=MAX_ATTEMPTS):
        """Send one logical request with bounded retries. Returns a 2xx Response.

        Retries (up to MAX_ATTEMPTS total): timeouts/connection errors, 5xx, 429.
        Never retries other 4xx: those are domain/auth errors and would fail again.
        """
        url = self.base_url + path
        for attempt in range(1, max_attempts + 1):
            delay = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
            try:
                resp = self._session.request(method, url, params=params, json=json,
                                             headers=headers, timeout=TIMEOUT)
            except (requests.Timeout, requests.ConnectionError) as exc:
                error = TrendyolError(ErrorCode.NETWORK_ERROR, retryable=True)
                reason = type(exc).__name__
            else:
                if resp.ok:
                    return resp
                error = normalize_error(resp)
                if not error.retryable or error.code == ErrorCode.ACCESS_BLOCKED:
                    raise error
                reason = f"HTTP {resp.status_code}"
                if resp.status_code == 429:
                    retry_after = _retry_after_seconds(resp)
                    if retry_after is not None:
                        delay = retry_after

            if attempt == max_attempts:
                logger.error("Trendyol %s %s failed after %d attempts: %s",
                             method, path, attempt, reason)
                raise error
            logger.warning("Trendyol %s %s attempt %d/%d failed (%s); retrying in %.1fs",
                           method, path, attempt, max_attempts, reason, delay)
            self._sleep(delay)

    def fetch_waiting_questions(self, page=0, size=MAX_PAGE_SIZE, *, barcode=None,
                                start_date=None, end_date=None,
                                order_by_field="CreatedDate", order_by_direction="ASC"):
        """Fetch ONE page of WAITING_FOR_ANSWER questions (page is 0-based).

        Optional documented filters: barcode, start_date/end_date (epoch ms; Trendyol
        defaults to the last week and caps a range at two weeks). They are omitted from
        the request when None, so the default call is unchanged.
        Returns items already mapped to crud.upsert_questions() input, plus paging info.
        Walking all pages is the caller's (scheduler's) job.
        """
        if not isinstance(page, int) or page < 0:
            raise ValueError("page must be an int >= 0")
        if not isinstance(size, int) or not 1 <= size <= MAX_PAGE_SIZE:
            raise ValueError(f"size must be an int between 1 and {MAX_PAGE_SIZE}")
        if order_by_field not in ORDER_BY_FIELDS:
            raise ValueError(f"order_by_field must be one of {ORDER_BY_FIELDS}")
        if order_by_direction not in ORDER_BY_DIRECTIONS:
            raise ValueError(f"order_by_direction must be one of {ORDER_BY_DIRECTIONS}")
        if barcode is not None and not (isinstance(barcode, str) and barcode.strip()):
            raise ValueError("barcode must be a non-empty string")
        for name, value in (("start_date", start_date), ("end_date", end_date)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                      or value < 0):
                raise ValueError(f"{name} must be epoch milliseconds (int >= 0)")
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("start_date must not be after end_date")

        params = {
            "supplierId": self.supplier_id,
            "status": "WAITING_FOR_ANSWER",
            "page": page,
            "size": size,
            # Stable order so page N means the same thing across calls.
            "orderByField": order_by_field,
            "orderByDirection": order_by_direction,
        }
        for key, value in (("barcode", barcode and barcode.strip()),
                           ("startDate", start_date), ("endDate", end_date)):
            if value is not None:
                params[key] = value
        resp = self._request("GET", f"{self._qna_path}/filter", params=params)
        body = resp.json()
        return {
            "questions": [self._map_question(item) for item in body.get("content") or []],
            "page": body.get("page", page),
            "size": body.get("size", size),
            "total_elements": body.get("totalElements", 0),
            "total_pages": body.get("totalPages", 0),
        }

    def _map_question(self, item):
        product_main_id = item.get("productMainId")
        return {
            "trendyol_question_id": item["id"],
            "supplier_id": self.supplier_id,
            "product_name": item.get("productName"),
            "product_main_id": str(product_main_id) if product_main_id is not None else None,
            "barcode": item.get("barcode"),  # not in the documented response; kept if sent
            "customer_name": item.get("userName"),
            "question_text": item.get("text") or "",
            "trendyol_status": item.get("status"),
            "question_created_at": _from_millis(item.get("creationDate")),
            "raw_payload": item,
        }

    def get_question(self, question_id):
        """GET one question by its Trendyol id (on-demand; the poll never calls this).

        Same Question schema as the list endpoint. Returns {"http_status", "question",
        "correlation_id"} where question is mapped like a list item (raw_payload = the
        full detail object). Raises TrendyolError; a 200 that is not a JSON object with
        an id is reported as UNKNOWN_ERROR instead of being trusted.
        """
        if not isinstance(question_id, int) or isinstance(question_id, bool) or question_id < 1:
            raise ValueError("question_id must be a positive int")
        resp = self._request("GET", f"{self._qna_path}/{question_id}")
        body = _json_or_none(resp)
        if not isinstance(body, dict) or body.get("id") is None:
            logger.warning("Trendyol question %s: unexpected 200 body (not a question object)",
                           question_id)
            raise TrendyolError(ErrorCode.UNKNOWN_ERROR, status_code=resp.status_code,
                                detail="Response is not a valid question object",
                                correlation_id=_correlation_id(resp))
        return {"http_status": resp.status_code, "question": self._map_question(body),
                "correlation_id": _correlation_id(resp)}

    def send_answer(self, question_id, text):
        """Validate locally, then POST the answer.

        Returns {"question_id", "answer_id", "text", "http_status", "response"}; `response`
        is Trendyol's parsed JSON body (or None), so nothing it sent back is dropped here.
        Raises TrendyolError (validation codes have status_code=None and no HTTP call is made);
        ValueError for an invalid question id (also before any HTTP call).
        """
        if not isinstance(question_id, int) or isinstance(question_id, bool) or question_id < 1:
            raise ValueError("question_id must be a positive int")
        text = validate_answer_text(text)
        # Retrying this POST is safe: Trendyol refuses a second answer to the same question
        # ("Cevaplanmış sorular tekrar cevaplanamaz"), so a retry cannot double-post.
        resp = self._request("POST", f"{self._qna_path}/{question_id}/answers",
                             json={"text": text})
        # Docs disagree on the success body ({"answerId": n} vs a plain message string);
        # an unreadable 2xx body is still a success: the HTTP status says it was accepted.
        body = _json_or_none(resp)
        return {"question_id": question_id, "answer_id": _answer_id(body), "text": text,
                "http_status": resp.status_code, "response": body}

    def create_stage_question(self, content_id, text, user_id, user_full_name,
                              show_user_name, channel_id):
        """STAGE ONLY: create a test customer question for a product (content id).

        Refuses (no HTTP call) unless TY_BASE_URL is Trendyol Stage. Sent once, never
        retried: a retried create could leave duplicate test questions behind.
        Returns {"http_status", "question_id", "correlation_id"}; raises TrendyolError.
        """
        if not self.is_stage:
            raise ValueError(f"create_stage_question is only allowed against {STAGE_HOST}")

        resp = self._request("POST", self._qna_path, max_attempts=1, json={
            "contentId": content_id,
            "text": text,
            "userId": user_id,
            "userFullName": user_full_name,
            "showUserName": show_user_name,
            "channelId": channel_id,
        })
        body = _json_or_none(resp)
        question_id = None
        if isinstance(body, dict):
            question_id = body.get("questionId", body.get("id"))
        elif isinstance(body, int):
            question_id = body
        return {"http_status": resp.status_code, "question_id": question_id,
                "correlation_id": _correlation_id(resp)}

    def get_product_by_barcode(self, barcode):
        """GET one product by barcode (Product V2 base information endpoint).

        Returns {"http_status", "barcode", "content_id", "product_name", "correlation_id"}.
        The documented V2 response has no title field; product_name is filled only if
        Trendyol sends one (title/productName/name).
        """
        barcode = (barcode or "").strip()
        if not barcode:
            raise ValueError("barcode is required")
        headers = {"storeFrontCode": self.storefront_code} if self.storefront_code else None
        resp = self._request(
            "GET",
            f"/integration/product/sellers/{self.supplier_id}/product/{quote(barcode, safe='')}",
            headers=headers,
        )
        body = _json_or_none(resp)
        if isinstance(body, dict) and isinstance(body.get("content"), list):
            body = body["content"][0] if body["content"] else {}  # list-shaped response
        body = body if isinstance(body, dict) else {}
        return {
            "http_status": resp.status_code,
            "barcode": body.get("barcode", barcode),
            "content_id": body.get("contentId", body.get("productContentId")),
            "product_name": body.get("title") or body.get("productName") or body.get("name"),
            "correlation_id": _correlation_id(resp),
        }
