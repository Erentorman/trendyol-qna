"""CLI helpers against a FAKE Stage client. This is local simulation, not a Stage test."""

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import create_app
from app.trendyol_client import ErrorCode, TrendyolError

STAGE = "https://stageapigw.trendyol.com"


class FakeStage:
    """Mimics TrendyolClient: one waiting question; answering removes it from the list
    unless `lag` is set (Trendyol still lists it as WAITING right after answering)."""

    def __init__(self, qid=424242, base_url=STAGE, lag=False, send_error=None):
        self.base_url = base_url
        self.user_agent = "1234 - SelfIntegration"
        self.qid = qid
        self.waiting = True
        self.lag = lag
        self.send_error = send_error
        self.sent = []

    def fetch_waiting_questions(self, page=0, size=50):
        items = []
        if self.waiting:
            items = [{"trendyol_question_id": self.qid, "supplier_id": 1234,
                      "question_text": "Stage test sorusu", "trendyol_status": "WAITING_FOR_ANSWER",
                      "question_created_at": datetime(2026, 9, 28, tzinfo=timezone.utc),
                      "raw_payload": {"id": self.qid, "status": "WAITING_FOR_ANSWER"}}]
        return {"questions": items, "page": page, "size": size,
                "total_elements": len(items), "total_pages": 1 if items else 0}

    def send_answer(self, question_id, text):
        self.sent.append((question_id, text))
        if self.send_error:
            raise self.send_error
        if not self.lag:
            self.waiting = False
        return {"question_id": question_id, "answer_id": 777, "text": text}


@pytest.fixture
def cli(Session):
    def _make(client, **config_overrides):
        config = SimpleNamespace(DATABASE_URL=os.environ["TEST_DATABASE_URL"],
                                 TY_SUPPLIER_ID="1234", TY_API_KEY="KEY-SECRET-1",
                                 TY_API_SECRET="SECRET-SECRET-2", TY_BASE_URL=STAGE,
                                 TY_USER_AGENT=None, POLL_INTERVAL_SECONDS=None)
        for k, v in config_overrides.items():
            setattr(config, k, v)
        app = create_app(config=config, trendyol_client=client)
        runner = app.test_cli_runner()
        return lambda *args: runner.invoke(args=["qna", *args])
    return _make


def payload(result):
    text = result.stdout
    return json.loads(text[text.index("{") if text.lstrip().startswith("{") else text.index("["):])


def test_config_check_never_prints_secrets(cli):
    result = cli(FakeStage())("config-check")
    assert result.exit_code == 0
    assert "TY_API_KEY: set" in result.stdout and "TY_USER_AGENT: empty" in result.stdout
    assert "(STAGE)" in result.stdout and "User-Agent: 1234 - SelfIntegration" in result.stdout
    assert "KEY-SECRET-1" not in result.output and "SECRET-SECRET-2" not in result.output


def test_config_check_without_client(cli):
    result = cli(None, TY_API_KEY=None, TY_API_SECRET=None)("config-check")
    assert "TY_API_KEY: empty" in result.stdout and "client: NOT CONFIGURED" in result.stdout


def test_config_check_with_real_client_from_config(cli):
    # No injected client: create_app builds a real TrendyolClient (no HTTP call is made).
    result = cli(None)("config-check")
    assert "(STAGE)" in result.stdout
    assert "KEY-SECRET-1" not in result.output and "SECRET-SECRET-2" not in result.output


def test_poll_runs_and_question_commands(cli):
    run = cli(FakeStage())
    first = payload(run("poll"))
    assert (first["status"], first["fetched_count"], first["inserted_count"]) == ("SUCCESS", 1, 1)
    second = payload(run("poll"))
    assert (second["inserted_count"], second["updated_count"]) == (0, 0)

    runs = payload(run("runs", "--limit", "2"))
    assert [r["id"] for r in runs] == [second["id"], first["id"]]

    q = payload(run("question", "424242"))
    assert (q["row_count"], q["duplicate"], q["rows"][0]["local_status"]) == (1, False, "NEW")


def test_stage_e2e_dry_run_stops_before_sending(cli):
    fake = FakeStage()
    result = cli(fake)("stage-e2e", "--question-id", "424242", "--answer", "Evet, stokta mevcut.")
    report = payload(result)
    assert result.exit_code == 0 and report["passed"] is True and report["mode"] == "dry-run"
    assert [s["step"] for s in report["steps"]] == [
        "poll_1", "single_row_new", "visible_in_html", "short_answer_rejected", "send_skipped"]
    assert fake.sent == []


@pytest.mark.parametrize("lag,final_status", [(False, "ANSWERED"), (True, "REOPENED")])
def test_stage_e2e_send_full_flow(cli, lag, final_status):
    fake = FakeStage(lag=lag)
    result = cli(fake)("stage-e2e", "--question-id", "424242",
                       "--answer", "Evet, stokta mevcut.", "--send")
    report = payload(result)
    assert result.exit_code == 0, report
    steps = {s["step"]: s for s in report["steps"]}
    assert steps["valid_answer_sent"]["status_during_send"] == "ANSWERING"
    assert steps["valid_answer_sent"]["trendyol_result"]["answer_id"] == 777
    assert steps["duplicate_answer_blocked"]["error_code"] == "ALREADY_LOCKED"
    assert steps["duplicate_answer_blocked"]["trendyol_send_calls"] == 1
    assert steps["poll_2_no_duplicate"]["row_count"] == 1
    assert steps["poll_2_no_duplicate"]["local_status"] == final_status
    assert fake.sent == [(424242, "Evet, stokta mevcut.")]


def test_stage_e2e_reports_trendyol_error(cli):
    fake = FakeStage(send_error=TrendyolError(ErrorCode.FORBIDDEN_WORD, status_code=400))
    result = cli(fake)("stage-e2e", "--question-id", "424242",
                       "--answer", "Evet, stokta mevcut.", "--send")
    report = payload(result)
    assert result.exit_code == 1 and report["passed"] is False
    sent = next(s for s in report["steps"] if s["step"] == "valid_answer_sent")
    assert sent["trendyol_error"]["code"] == "FORBIDDEN_WORD"


def test_stage_e2e_refuses_non_stage_base_url(cli):
    fake = FakeStage(base_url="https://apigw.trendyol.com")
    result = cli(fake)("stage-e2e", "--question-id", "1", "--answer", "Evet, stokta mevcut.")
    assert result.exit_code != 0 and "Refusing to run" in result.output
    assert fake.sent == []


def test_stage_e2e_question_not_fetched(cli):
    result = cli(FakeStage(qid=1))("stage-e2e", "--question-id", "999", "--answer", "Evet, stokta mevcut.")
    report = payload(result)
    assert result.exit_code == 1 and report["steps"][-1]["step"] == "single_row_new"
    assert report["steps"][-1]["row_count"] == 0
