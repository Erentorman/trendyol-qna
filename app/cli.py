"""Manual / Stage test helpers: `flask --app app qna <command>`.

These talk to whatever TY_BASE_URL points at. They are never run by the test suite
against a real API; tests inject a fake client. Secrets are never printed.
"""

import json
from urllib.parse import urlparse

import click
from flask import current_app
from flask.cli import AppGroup
from sqlalchemy import select

from app.db import SessionLocal
from app.models import PollRun, Question
from app.scheduler import poll_job
from app.trendyol_client import STAGE_HOST, TrendyolError

qna_cli = AppGroup("qna", help="Manual and Stage test helpers.")

SETTINGS = ("TY_SUPPLIER_ID", "TY_API_KEY", "TY_API_SECRET", "TY_BASE_URL", "TY_USER_AGENT",
            "POLL_INTERVAL_SECONDS")


def _client():
    client = current_app.extensions.get("trendyol_client")
    if client is None:
        raise click.ClickException("Trendyol client is not configured; run `qna config-check`.")
    return client


def _iso(value):
    return value.isoformat() if value else None


def _run_dict(run):
    return {"id": run.id, "status": run.status, "fetched_count": run.fetched_count,
            "inserted_count": run.inserted_count, "updated_count": run.updated_count,
            "error_count": run.error_count, "duration_ms": run.duration_ms,
            "error_message": run.error_message, "started_at": _iso(run.started_at)}


def _question_dict(q):
    return {"id": q.id, "trendyol_question_id": q.trendyol_question_id,
            "local_status": q.local_status, "trendyol_status": q.trendyol_status,
            "retry_count": q.retry_count, "retryable": q.retryable, "error_code": q.error_code,
            "locked_at": _iso(q.locked_at), "answered_at": _iso(q.answered_at),
            "answer_id": q.answer_id}


def _rows(trendyol_question_id):
    with SessionLocal() as s:
        stmt = select(Question).where(Question.trendyol_question_id == trendyol_question_id)
        return list(s.scalars(stmt))


def _echo(data):
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


@qna_cli.command("config-check")
def config_check():
    """Show which Trendyol settings are set (values are never printed)."""
    config = current_app.extensions["config"]
    for name in SETTINGS:
        click.echo(f"{name}: {'set' if getattr(config, name, None) else 'empty'}")
    client = current_app.extensions.get("trendyol_client")
    if client is None:
        click.echo("client: NOT CONFIGURED")
        return
    host = urlparse(client.base_url).hostname
    click.echo(f"base_url host: {host} ({'STAGE' if host == STAGE_HOST else 'NOT STAGE'})")
    click.echo(f"User-Agent: {client.user_agent}")


@qna_cli.command("poll")
def poll():
    """Run one poll_job now and print its PollRun."""
    run = poll_job(_client(), SessionLocal)
    if run is None:
        raise click.ClickException("Poll crashed before a PollRun was saved; see logs.")
    _echo(_run_dict(run))


@qna_cli.command("runs")
@click.option("--limit", default=5, show_default=True)
def runs(limit):
    """Show the latest PollRuns."""
    with SessionLocal() as s:
        rows = s.scalars(select(PollRun).order_by(PollRun.id.desc()).limit(limit))
        _echo([_run_dict(r) for r in rows])


@qna_cli.command("question")
@click.argument("trendyol_question_id", type=int)
def question(trendyol_question_id):
    """Show DB state of a Trendyol question id (and whether duplicates exist)."""
    rows = _rows(trendyol_question_id)
    _echo({"row_count": len(rows), "duplicate": len(rows) > 1,
           "rows": [_question_dict(q) for q in rows]})


class _ObservingClient:
    """Wraps the real client for stage-e2e: records the DB status seen *during* send_answer
    (must be ANSWERING) and the normalized Trendyol outcome."""

    def __init__(self, inner, local_id):
        self._inner = inner
        self._local_id = local_id
        self.send_calls = 0
        self.status_during_send = None
        self.result = None
        self.error = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def send_answer(self, question_id, text):
        self.send_calls += 1
        with SessionLocal() as s:
            self.status_during_send = s.get(Question, self._local_id).local_status
        try:
            self.result = self._inner.send_answer(question_id, text)
            return self.result
        except TrendyolError as exc:
            self.error = exc.to_dict()
            raise


@qna_cli.command("stage-e2e")
@click.option("--question-id", "trendyol_question_id", type=int, required=True,
              help="Trendyol questionId of the test question created on Stage.")
@click.option("--answer", "answer_text", required=True, help="Valid answer (10-2000 chars).")
@click.option("--send/--dry-run", default=False,
              help="--send posts a real answer to Trendyol Stage. Default stops before it.")
def stage_e2e(trendyol_question_id, answer_text, send):
    """End-to-end check of one Stage question through poll, DB, HTML and /api/answer."""
    client = _client()
    host = urlparse(client.base_url).hostname
    if host != STAGE_HOST:
        raise click.ClickException(f"Refusing to run: TY_BASE_URL host is {host}, not {STAGE_HOST}.")

    report = {"question_id": trendyol_question_id, "mode": "send" if send else "dry-run",
              "steps": []}

    def step(name, ok, **detail):
        report["steps"].append({"step": name, "ok": bool(ok), **detail})
        return ok

    def finish():
        report["passed"] = all(s["ok"] for s in report["steps"])
        _echo(report)
        if not report["passed"]:
            raise SystemExit(1)

    # 1) Poll and find exactly one NEW row.
    run = poll_job(client, SessionLocal)
    rows = _rows(trendyol_question_id)
    step("poll_1", run is not None and run.status == "SUCCESS",
         poll_run=_run_dict(run) if run else None)
    if not step("single_row_new", len(rows) == 1 and rows[0].local_status == "NEW",
                row_count=len(rows), local_status=rows[0].local_status if rows else None):
        return finish()  # not fetched (maybe still in Trendyol's evaluation) or not NEW
    local_id = rows[0].id

    observing = _ObservingClient(client, local_id)
    current_app.extensions["trendyol_client"] = observing
    web = current_app.test_client()
    try:
        # 2) Visible on the HTML page.
        html = web.get("/").get_data(as_text=True)
        step("visible_in_html", f'data-question-id="{local_id}"' in html)

        # 3) Short answer rejected locally: 400, no Trendyol call, still NEW.
        resp = web.post("/api/answer", json={"question_id": local_id, "answer": "kısa"})
        body = resp.get_json()
        step("short_answer_rejected", resp.status_code == 400
             and body["error"]["code"] == "ANSWER_TOO_SHORT" and observing.send_calls == 0
             and _rows(trendyol_question_id)[0].local_status == "NEW",
             http_status=resp.status_code, error_code=body["error"]["code"])

        if not send:
            step("send_skipped", True, note="dry-run: rerun with --send to post the answer")
            return finish()

        # 4) Valid answer: NEW -> ANSWERING (seen during the call) -> ANSWERED.
        resp = web.post("/api/answer", json={"question_id": local_id, "answer": answer_text})
        row = _rows(trendyol_question_id)[0]
        step("valid_answer_sent", resp.status_code == 200 and row.local_status == "ANSWERED"
             and observing.status_during_send == "ANSWERING" and row.locked_at is None,
             http_status=resp.status_code, status_during_send=observing.status_during_send,
             local_status=row.local_status, trendyol_result=observing.result,
             trendyol_error=observing.error, response_error=(resp.get_json() or {}).get("error"))

        # 5) Second answer blocked locally, no second Trendyol call.
        resp = web.post("/api/answer", json={"question_id": local_id, "answer": answer_text})
        step("duplicate_answer_blocked", resp.status_code == 409 and observing.send_calls == 1,
             http_status=resp.status_code, error_code=resp.get_json()["error"]["code"],
             trendyol_send_calls=observing.send_calls)
    finally:
        current_app.extensions["trendyol_client"] = client

    # 6) Poll again: still exactly one row. Status is recorded (REOPENED is possible if
    #    Trendyol still lists the question as WAITING_FOR_ANSWER right after answering).
    run = poll_job(client, SessionLocal)
    rows = _rows(trendyol_question_id)
    step("poll_2_no_duplicate", run is not None and run.status == "SUCCESS" and len(rows) == 1,
         poll_run=_run_dict(run) if run else None, row_count=len(rows),
         local_status=rows[0].local_status if rows else None)
    return finish()
