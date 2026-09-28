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
    ErrorCode.UNKNOWN_ERROR: "Unexpected Trendyol error.",
}

# Substrings of Trendyol's (Turkish) error messages -> domain code. Order matters.
DOMAIN_PATTERNS = (
    (("daha önce cevaplan", "already answered"), ErrorCode.QUESTION_ALREADY_ANSWERED),
    (("süre limit", "time limit"), ErrorCode.TIME_LIMIT_EXCEEDED),
    (("yasaklı", "forbidden"), ErrorCode.FORBIDDEN_WORD),
    (("çok kısa", "too short"), ErrorCode.ANSWER_TOO_SHORT),
    (("çok uzun", "too long"), ErrorCode.ANSWER_TOO_LONG),
    (("cevap boş", "empty"), ErrorCode.EMPTY_ANSWER),
)


class TrendyolError(Exception):
    """Normalized Trendyol failure. The only error type this module raises for API calls."""

    def __init__(self, code, retryable=False, status_code=None):
        self.code = code
        self.message = MESSAGES[code]
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(f"{code}: {self.message}")

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "status_code": self.status_code,
        }


def _error_text(resp):
    """Collect human-readable message strings from an error body, whatever its shape."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text or ""
    parts = []
    if isinstance(body, str):
        parts.append(body)
    elif isinstance(body, dict):
        for key in ("message", "detail", "title", "exception"):
            if isinstance(body.get(key), str):
                parts.append(body[key])
        for err in body.get("errors") or []:
            if isinstance(err, dict):
                parts += [str(err[k]) for k in ("key", "message") if err.get(k)]
    return " | ".join(parts)


def normalize_error(resp):
    status = resp.status_code
    if status == 429:
        return TrendyolError(ErrorCode.RATE_LIMITED, retryable=True, status_code=status)
    if status >= 500:
        return TrendyolError(ErrorCode.SERVER_ERROR, retryable=True, status_code=status)

    text = _error_text(resp)
    lowered = text.lower()
    for needles, code in DOMAIN_PATTERNS:
        if any(n in lowered for n in needles):
            return TrendyolError(code, status_code=status)

    if status in (401, 403):
        return TrendyolError(ErrorCode.AUTH_ERROR, status_code=status)
    # Keep the original text in server logs only, truncated, for debugging.
    logger.warning("Unmapped Trendyol error status=%s body=%.200s", status, text)
    return TrendyolError(ErrorCode.UNKNOWN_ERROR, status_code=status)


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


def _from_millis(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


class TrendyolClient:
    def __init__(self, supplier_id, api_key, api_secret, base_url, user_agent=None,
                 session=None, sleep=time.sleep):
        self.supplier_id = int(supplier_id)
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
        return cls(config.TY_SUPPLIER_ID, config.TY_API_KEY, config.TY_API_SECRET,
                   config.TY_BASE_URL, config.TY_USER_AGENT, **kwargs)

    def __repr__(self):
        return f"TrendyolClient(supplier_id={self.supplier_id}, base_url={self.base_url!r})"

    @property
    def _qna_path(self):
        return f"/integration/qna/sellers/{self.supplier_id}/questions"

    def _request(self, method, path, *, params=None, json=None):
        """Send one logical request with bounded retries. Returns a 2xx Response.

        Retries (up to MAX_ATTEMPTS total): timeouts/connection errors, 5xx, 429.
        Never retries other 4xx: those are domain/auth errors and would fail again.
        """
        url = self.base_url + path
        for attempt in range(1, MAX_ATTEMPTS + 1):
            delay = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
            try:
                resp = self._session.request(method, url, params=params, json=json,
                                             timeout=TIMEOUT)
            except (requests.Timeout, requests.ConnectionError) as exc:
                error = TrendyolError(ErrorCode.NETWORK_ERROR, retryable=True)
                reason = type(exc).__name__
            else:
                if resp.ok:
                    return resp
                error = normalize_error(resp)
                if not error.retryable:
                    raise error
                reason = f"HTTP {resp.status_code}"
                if resp.status_code == 429:
                    retry_after = _retry_after_seconds(resp)
                    if retry_after is not None:
                        delay = retry_after

            if attempt == MAX_ATTEMPTS:
                logger.error("Trendyol %s %s failed after %d attempts: %s",
                             method, path, attempt, reason)
                raise error
            logger.warning("Trendyol %s %s attempt %d/%d failed (%s); retrying in %.1fs",
                           method, path, attempt, MAX_ATTEMPTS, reason, delay)
            self._sleep(delay)

    def fetch_waiting_questions(self, page=0, size=MAX_PAGE_SIZE):
        """Fetch ONE page of WAITING_FOR_ANSWER questions (page is 0-based).

        Returns items already mapped to crud.upsert_questions() input, plus paging info.
        Walking all pages is the caller's (scheduler's) job.
        """
        if not isinstance(page, int) or page < 0:
            raise ValueError("page must be an int >= 0")
        if not isinstance(size, int) or not 1 <= size <= MAX_PAGE_SIZE:
            raise ValueError(f"size must be an int between 1 and {MAX_PAGE_SIZE}")

        resp = self._request("GET", f"{self._qna_path}/filter", params={
            "supplierId": self.supplier_id,
            "status": "WAITING_FOR_ANSWER",
            "page": page,
            "size": size,
            # Stable order so page N means the same thing across calls.
            "orderByField": "CreatedDate",
            "orderByDirection": "ASC",
        })
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

    def send_answer(self, question_id, text):
        """Validate locally, then POST the answer. Returns {"question_id", "answer_id", "text"}.

        Raises TrendyolError (validation codes have status_code=None and no HTTP call is made).
        """
        text = validate_answer_text(text)
        resp = self._request("POST", f"{self._qna_path}/{question_id}/answers",
                             json={"text": text})
        # Docs disagree on the success body ({"answerId": n} vs a plain message string).
        try:
            body = resp.json()
        except ValueError:
            body = None
        answer_id = body.get("answerId") if isinstance(body, dict) else None
        return {"question_id": question_id, "answer_id": answer_id, "text": text}
