/* Stage test tools. Talks only to our backend (/api/stage/*), never to Trendyol. */
(function () {
  "use strict";

  const BUILDERS = {
    product: function (f) { return { barcode: f.barcode.value.trim() }; },
    question: function (f) {
      return { content_id: Number(f.content_id.value), text: f.text.value.trim(),
               user_id: Number(f.user_id.value), user_full_name: f.user_full_name.value.trim() };
    },
    poll: function () { return {}; },
  };

  const SUCCESS_ROWS = {
    product: function (d) {
      return [["Durum", "BAŞARILI"], ["HTTP Status", d.http_status],
              ["Product Name", d.product_name ?? "(yanıtta yok)"], ["Barcode", d.barcode],
              ["Content ID", d.content_id ?? "(yanıtta yok)"], ["Correlation ID", d.correlation_id]];
    },
    question: function (d) {
      return [["Durum", "BAŞARILI"], ["HTTP Status", d.http_status],
              ["Question ID", d.question_id ?? "(yanıtta yok)"], ["Correlation ID", d.correlation_id]];
    },
    poll: function (d) {
      const r = d.poll_run;
      return [["Durum", r.status === "SUCCESS" ? "BAŞARILI" : "HATA"], ["Fetched", r.fetched_count],
              ["Inserted", r.inserted_count], ["Updated", r.updated_count],
              ["Error Count", r.error_count], ["Süre (ms)", r.duration_ms], ["Hata", r.error_message]];
    },
  };

  // All server text goes through textContent: never parsed as HTML.
  function renderResult(box, kind, rows) {
    const dl = box.querySelector("dl");
    dl.textContent = "";
    rows.forEach(function (row) {
      if (row[1] === null || row[1] === undefined || row[1] === "") return;
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = row[0];
      dd.textContent = String(row[1]);
      dl.append(dt, dd);
    });
    box.className = "result is-" + kind;
    box.hidden = false;
  }

  function renderConnectivity(conn) {
    if (!conn) return;
    Object.keys(conn).forEach(function (key) {
      const li = document.querySelector('[data-conn="' + key + '"]');
      if (!li) return;
      li.className = "conn-" + String(conn[key].state).toLowerCase();
      li.querySelector('[data-role="conn-summary"]').textContent = conn[key].summary;
    });
  }

  // Same Istanbul local time the server-rendered rows use.
  function formatDate(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "-";
    return d.toLocaleString("tr-TR", { timeZone: "Europe/Istanbul", day: "2-digit", month: "2-digit",
                                        year: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  // Debug history rows. No answer link on purpose: these are not Trendyol-confirmed questions.
  function prependTestQuestion(t) {
    const tbody = document.getElementById("test-history-rows");
    if (!tbody) return;
    const empty = tbody.querySelector(".empty-row");
    if (empty) empty.remove();
    const tr = tbody.insertRow(0);
    tr.dataset.testQuestionId = t.question_id;
    [t.question_id, t.text, t.content_id, t.user_id, t.user_full_name, formatDate(t.created_at),
     t.create_http_status].forEach(function (v) { tr.insertCell().textContent = String(v); });
    const label = tr.insertCell();
    label.className = "test-label";
    label.textContent = t.label;
    document.getElementById("test-history-count").textContent = String(tbody.rows.length);
  }

  function safeHttps(url) {
    if (typeof url !== "string" || !url.startsWith("https://")) return null;
    try { return new URL(url).protocol === "https:" ? url : null; } catch (_) { return null; }
  }

  function el(tag, props, text) {
    const e = document.createElement(tag);
    Object.assign(e, props || {});
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function statusBadge(status) {
    return el("span", { className: "status status-" + String(status).toLowerCase() }, status);
  }

  // Every server string goes through textContent / DOM properties, never innerHTML.
  function renderQuestions(questions) {
    const tbody = document.getElementById("stage-questions");
    tbody.textContent = "";
    document.getElementById("stage-question-count").textContent = String(questions.length);
    if (!questions.length) {
      const tr = tbody.insertRow();
      tr.className = "empty-row";
      const td = tr.insertCell();
      td.colSpan = 8;
      td.textContent = "Cevap bekleyen soru yok.";
      return;
    }
    questions.forEach(function (q) {
      const tr = tbody.insertRow();
      tr.dataset.questionId = q.id;
      const ty = q.trendyol_status || q.local_status;
      const webUrl = safeHttps(q.web_url);
      const imageUrl = safeHttps(q.image_url);

      tr.insertCell().textContent = String(q.id);
      tr.insertCell().textContent = String(q.trendyol_question_id);

      const st = tr.insertCell();
      st.appendChild(statusBadge(ty));
      if (q.trendyol_status && q.local_status !== q.trendyol_status) {
        st.appendChild(el("small", { className: "local-status" }, "yerel: " + q.local_status));
      }

      const text = tr.insertCell();
      text.className = "q-text";
      text.appendChild(document.createTextNode(q.question_text));
      const details = el("details", { className: "q-detail" });
      details.appendChild(el("summary", {}, "Detay"));
      const dl = el("dl");
      [["Kullanıcı", q.customer_name || "-"], ["answer", q.answer], ["rejectedAnswer", q.rejected_answer],
       ["webUrl", webUrl], ["imageUrl", imageUrl]].forEach(function (row) {
        if (!row[1]) return;
        dl.append(el("dt", {}, row[0]), el("dd", {}, row[1]));
      });
      details.appendChild(dl);
      text.appendChild(details);

      const prod = tr.insertCell();
      prod.className = "q-product";
      if (imageUrl) {
        prod.appendChild(el("img", { className: "thumb", src: imageUrl, alt: "", width: 40, height: 40,
                                     loading: "lazy", referrerPolicy: "no-referrer" }));
      }
      if (webUrl) {
        prod.appendChild(el("a", { href: webUrl, target: "_blank", rel: "noopener noreferrer" },
                            q.product_name || "Ürünü aç"));
      } else {
        prod.appendChild(document.createTextNode(q.product_name || "-"));
      }
      if (q.product_main_id) prod.appendChild(el("small", { className: "local-status" }, "Ana ID: " + q.product_main_id));

      tr.insertCell().textContent = q.customer_name || "-";
      tr.insertCell().textContent = formatDate(q.question_created_at);

      const action = tr.insertCell();
      if (ty === "WAITING_FOR_ANSWER") {
        action.appendChild(el("a", { className: "btn-answer", href: "/#q-" + encodeURIComponent(q.id) }, "Cevapla"));
      } else {
        action.appendChild(statusBadge(ty));
      }
    });
  }

  // Answer objects are shown by their text when they have one, else as compact JSON.
  function describe(value) {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === "object") {
      return typeof value.text === "string" && value.text ? value.text : JSON.stringify(value);
    }
    return value;
  }

  function detailRows(d) {
    return [["Question ID", d.question_id], ["Trendyol Status", d.status], ["Soru", d.text],
            ["creationDate", formatDate(d.creation_date)], ["productName", d.product_name],
            ["productMainId", d.product_main_id], ["userName", d.user_name],
            ["showUserName", d.show_user_name], ["webUrl", d.web_url], ["imageUrl", d.image_url],
            ["answer", describe(d.answer)], ["rejectedAnswer", describe(d.rejected_answer)],
            ["HTTP Status", d.http_status], ["Correlation ID", d.correlation_id]];
  }

  const detailForm = document.getElementById("detail-form");
  if (detailForm) {
    const button = detailForm.querySelector('button[type="submit"]');
    const box = document.getElementById("detail-result");
    const flag = document.getElementById("detail-answerable");
    const raw = document.getElementById("detail-raw");
    const label = button.textContent;

    detailForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (button.disabled) return;
      const id = detailForm.elements.question_id.value.trim();
      flag.textContent = "";
      raw.hidden = true;
      if (!/^[1-9][0-9]*$/.test(id)) {
        renderResult(box, "error", [["Durum", "HATA"], ["Error Code", "INVALID_REQUEST"],
                                    ["Message", "Question ID pozitif bir tam sayı olmalı."]]);
        return;
      }
      button.disabled = true;
      button.textContent = "Getiriliyor…";
      try {
        const resp = await fetch(detailForm.dataset.url + id, { headers: { Accept: "application/json" } });
        let body = null;
        try { body = await resp.json(); } catch (_) { body = null; }
        if (body) renderConnectivity(body.connectivity);
        if (body && body.ok) {
          const d = body.data;
          renderResult(box, d.answerable ? "success" : "error", detailRows(d));
          flag.textContent = d.answerable ? "Cevaplanabilir"
                                          : "Cevaplanamaz - Trendyol status: " + d.status;
          raw.querySelector("pre").textContent = JSON.stringify(d.raw, null, 2);
          raw.hidden = false;
        } else {
          const err = (body && body.error) || { code: "BAD_RESPONSE", message: "Sunucudan beklenmeyen yanıt." };
          renderResult(box, "error", [["Durum", "HATA"], ["HTTP Status", err.http_status ?? resp.status],
                                      ["Error Code", err.code], ["Message", err.message],
                                      ["Correlation ID", err.correlation_id]]);
        }
      } catch (_) {
        renderResult(box, "error", [["Durum", "HATA"], ["Error Code", "CLIENT_NETWORK"],
                                    ["Message", "Sunucuya ulaşılamadı."]]);
      } finally {
        button.disabled = false;
        button.textContent = label;
      }
    });
  }

  document.querySelectorAll("form[data-tool]").forEach(function (form) {
    const tool = form.dataset.tool;
    const button = form.querySelector('button[type="submit"]');
    const box = document.querySelector('[data-result="' + tool + '"]');
    const label = button.textContent;

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (button.disabled) return;
      button.disabled = true;
      button.textContent = "Gönderiliyor…";
      try {
        const resp = await fetch(form.dataset.url, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify(BUILDERS[tool](form.elements)),
        });
        let body = null;
        try { body = await resp.json(); } catch (_) { body = null; }
        if (body) renderConnectivity(body.connectivity);

        if (body && body.ok) {
          renderResult(box, body.data.poll_run && body.data.poll_run.status !== "SUCCESS" ? "error" : "success",
                       SUCCESS_ROWS[tool](body.data));
          if (tool === "poll") renderQuestions(body.data.questions);
          if (tool === "question" && body.data.test_question) prependTestQuestion(body.data.test_question);
        } else {
          const err = (body && body.error) || { code: "BAD_RESPONSE", message: "Sunucudan beklenmeyen yanıt." };
          renderResult(box, "error", [["Durum", "HATA"], ["HTTP Status", err.http_status ?? resp.status],
                                      ["Error Code", err.code], ["Message", err.message],
                                      ["Correlation ID", err.correlation_id]]);
        }
      } catch (_) {
        renderResult(box, "error", [["Durum", "HATA"], ["Error Code", "CLIENT_NETWORK"],
                                    ["Message", "Sunucuya ulaşılamadı."]]);
      } finally {
        button.disabled = false;
        button.textContent = label;
      }
    });
  });
})();
