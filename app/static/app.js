/* Answer form behaviour for the question list. Vanilla JS, no build step.
 *
 * The frontend only improves UX (validation, counter, no double submit).
 * The real safety net is the backend's atomic lock in POST /api/answer.
 * Exported as a CommonJS module under Node so the logic can be unit tested.
 */
(function () {
  "use strict";

  const REQUEST_TIMEOUT_MS = 120000; // backend retries can take ~1 minute

  const MESSAGES = {
    SUCCESS: "Cevabınız Trendyol'a gönderildi.",
    SENDING: "Cevap gönderiliyor… Trendyol yanıtı birkaç saniye sürebilir, lütfen bekleyin.",
    EMPTY_ANSWER: "Cevap boş olamaz.",
    ANSWER_TOO_SHORT: "Cevap en az 10 karakter olmalı.",
    ANSWER_TOO_LONG: "Cevap en fazla 2000 karakter olabilir.",
    ALREADY_LOCKED: "Bu soru şu anda cevaplanamıyor: başka bir işlemde ya da zaten cevaplanmış. Güncel durum için sayfayı yenileyin.",
    QUESTION_ALREADY_ANSWERED: "Bu soru Trendyol'da zaten cevaplanmış; soru kapatıldı.",
    TIME_LIMIT_EXCEEDED: "Bu sorunun cevaplama süresi dolmuş; soru kapatıldı.",
    FORBIDDEN_WORD: "Cevap Trendyol'un yasaklı kelime kontrolüne takıldı. Trendyol hâlâ cevap bekliyorsa soru bir sonraki senkronizasyonda tekrar açılır; cevabı düzenleyip yeniden gönderebilirsiniz.",
    RATE_LIMITED: "Trendyol istek limiti aşıldı. Soru bir sonraki senkronizasyonda tekrar açılacak; birkaç dakika sonra yeniden deneyin.",
    SERVER_ERROR: "Trendyol şu anda yanıt vermiyor. Soru bir sonraki senkronizasyonda tekrar açılacak.",
    NETWORK_ERROR: "Trendyol'a bağlanılamadı (zaman aşımı). Soru bir sonraki senkronizasyonda tekrar açılacak.",
    AUTH_ERROR: "Trendyol API kimlik bilgileri reddedildi. Yöneticiye haber verin.",
    TRENDYOL_NOT_CONFIGURED: "Trendyol API ayarları eksik; cevap gönderilemiyor.",
    QUESTION_NOT_FOUND: "Soru bulunamadı. Sayfayı yenileyin.",
    INTERNAL_ERROR: "Beklenmeyen bir sunucu hatası oluştu.",
    CLIENT_NETWORK: "Sunucuya ulaşılamadı. Cevabın gidip gitmediğini görmek için sayfayı yenileyin.",
    CLIENT_TIMEOUT: "Sunucu zamanında yanıt vermedi. Cevabın gidip gitmediğini görmek için sayfayı yenileyin.",
    BAD_RESPONSE: "Sunucudan beklenmeyen bir yanıt geldi. Sayfayı yenileyin.",
    UNKNOWN: "Cevap gönderilemedi.",
  };

  // Errors returned BEFORE the backend took the lock: state is unchanged, the user
  // can fix the text and resend from the same card. Any other error means the
  // question moved on (FAILED/CLOSED/locked) or its state is unknown.
  const EDITABLE_ERRORS = new Set([
    "EMPTY_ANSWER", "ANSWER_TOO_SHORT", "ANSWER_TOO_LONG", "INVALID_REQUEST",
    "TRENDYOL_NOT_CONFIGURED",
  ]);

  // Count like the backend (Python len on the stripped text): code points, not UTF-16 units.
  function charCount(text) {
    return Array.from((text || "").trim()).length;
  }

  function validateAnswer(text, min, max) {
    const n = charCount(text);
    if (n === 0) return { ok: false, code: "EMPTY_ANSWER" };
    if (n < min) return { ok: false, code: "ANSWER_TOO_SHORT" };
    if (n > max) return { ok: false, code: "ANSWER_TOO_LONG" };
    return { ok: true, code: null };
  }

  function counterLabel(text, min, max) {
    const n = charCount(text);
    return { text: n + " / " + max, invalid: n > 0 && (n < min || n > max) };
  }

  // Only our own mapped texts or the backend's normalized message are ever shown.
  function messageFor(error) {
    const code = error && error.code;
    if (code && MESSAGES[code]) return MESSAGES[code];
    return (error && typeof error.message === "string" && error.message) || MESSAGES.UNKNOWN;
  }

  async function postAnswer(fetchImpl, questionId, answer) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const resp = await fetchImpl("/api/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ question_id: questionId, answer: answer }),
        signal: controller.signal,
      });
      let body = null;
      try {
        body = await resp.json();
      } catch (_) {
        body = null; // e.g. an HTML error page from a proxy: never displayed
      }
      if (body && typeof body.ok === "boolean") return body;
      return { ok: false, error: { code: "BAD_RESPONSE" } };
    } catch (err) {
      const code = err && err.name === "AbortError" ? "CLIENT_TIMEOUT" : "CLIENT_NETWORK";
      return { ok: false, error: { code: code } };
    } finally {
      clearTimeout(timer);
    }
  }

  function setupCard(card, opts) {
    const form = card.querySelector(".answer-form");
    const textarea = card.querySelector("textarea");
    const button = card.querySelector('[data-role="submit"]');
    const counter = card.querySelector('[data-role="counter"]');
    const message = card.querySelector('[data-role="message"]');
    const status = card.querySelector(".status");
    const { fetchImpl, min, max, onAnswered } = opts;
    let inFlight = false; // per-card guard: blocks double click / double submit
    let done = false;

    function show(kind, text) {
      message.className = "message is-" + kind;
      message.textContent = text;
    }

    function refresh() {
      const c = counterLabel(textarea.value, min, max);
      counter.textContent = c.text;
      counter.classList.toggle("is-invalid", c.invalid);
      if (!inFlight && !done) button.disabled = !validateAnswer(textarea.value, min, max).ok;
    }

    function setLoading(loading) {
      button.disabled = loading || done;
      textarea.readOnly = loading || done;
      button.classList.toggle("is-loading", loading);
      button.textContent = loading ? "Gönderiliyor…" : "Cevapla";
    }

    function finish() {
      done = true;
      card.classList.add("is-done");
      setLoading(false);
    }

    textarea.addEventListener("input", refresh);

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (inFlight || done) return;

      const check = validateAnswer(textarea.value, min, max);
      if (!check.ok) {
        show("error", messageFor(check));
        return;
      }

      inFlight = true;
      setLoading(true);
      show("info", MESSAGES.SENDING);

      const result = await postAnswer(fetchImpl, Number(card.dataset.questionId), textarea.value.trim());
      inFlight = false;

      if (result.ok) {
        card.dataset.status = "ANSWERED";
        status.textContent = "ANSWERED";
        status.className = "status status-answered";
        finish();
        show("success", MESSAGES.SUCCESS);
        if (onAnswered) onAnswered(card);
        return;
      }

      const code = result.error && result.error.code;
      if (EDITABLE_ERRORS.has(code)) {
        setLoading(false);
        refresh();
      } else {
        finish();
      }
      show("error", messageFor(result.error));
    });

    refresh();
  }

  function init(doc, fetchImpl) {
    const list = doc.getElementById("questions");
    if (!list) return;
    const min = Number(list.dataset.min) || 10;
    const max = Number(list.dataset.max) || 2000;
    const openCount = doc.getElementById("open-count");
    list.querySelectorAll(".card").forEach(function (card) {
      setupCard(card, {
        fetchImpl: fetchImpl,
        min: min,
        max: max,
        onAnswered: function () {
          if (openCount) openCount.textContent = String(Math.max(0, Number(openCount.textContent) - 1));
        },
      });
    });
  }

  const api = { MESSAGES, EDITABLE_ERRORS, charCount, validateAnswer, counterLabel, messageFor,
                postAnswer, setupCard, init };

  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    init(document, window.fetch.bind(window)); // script is loaded at the end of <body>
  }
})();
