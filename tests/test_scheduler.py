import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app import scheduler as sched_mod
from app.config import ConfigError
from app.models import PollRun, Question
from app.scheduler import POLL_JOB_ID, poll_job, start_scheduler, stop_scheduler
from app.trendyol_client import ErrorCode, TrendyolError


def item(tid, status="WAITING_FOR_ANSWER", text="Kargo ne zaman?"):
    return {
        "trendyol_question_id": tid,
        "supplier_id": 1234,
        "question_text": text,
        "trendyol_status": status,
        "question_created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "raw_payload": {"id": tid, "text": text, "status": status},
    }


class FakeClient:
    """Stands in for TrendyolClient: serves pre-built pages or raises at a given page."""

    def __init__(self, pages, fail_at=None, error=None, total_pages=None):
        self.pages = pages
        self.fail_at = fail_at
        self.error = error or TrendyolError(ErrorCode.SERVER_ERROR, retryable=True, status_code=503)
        self.total_pages = len(pages) if total_pages is None else total_pages
        self.calls = []

    def fetch_waiting_questions(self, page=0, size=50):
        self.calls.append((page, size))
        if page == self.fail_at:
            raise self.error
        questions = self.pages[page] if page < len(self.pages) else []
        return {"questions": questions, "page": page, "size": size,
                "total_elements": sum(map(len, self.pages)), "total_pages": self.total_pages}


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_single_page_success(Session):
    client = FakeClient([[item(1), item(2)]])
    run = poll_job(client, Session)

    assert client.calls == [(0, 50)]
    with Session() as s:
        stored = s.get(PollRun, run.id)
        assert stored.status == "SUCCESS"
        assert stored.error_message is None
        assert (stored.fetched_count, stored.inserted_count, stored.updated_count,
                stored.error_count) == (2, 2, 0, 0)
        assert stored.finished_at >= stored.started_at
        assert stored.duration_ms is not None and stored.duration_ms >= 0
        assert count(s, Question) == 2


def test_multi_page_walks_all_pages_and_stops_at_last(Session):
    client = FakeClient([[item(1), item(2)], [item(3), item(4)], [item(5)]])
    run = poll_job(client, Session, page_size=2)

    assert client.calls == [(0, 2), (1, 2), (2, 2)]  # no request for a 4th page
    assert (run.status, run.fetched_count, run.inserted_count) == ("SUCCESS", 5, 5)


def test_repeated_poll_no_duplicates_and_counters(Session):
    poll_job(FakeClient([[item(1), item(2)]]), Session)
    # Same two questions again, one with changed text, plus one new question.
    run = poll_job(FakeClient([[item(1), item(2, text="Kargo kaç günde?"), item(3)]]), Session)

    assert (run.fetched_count, run.inserted_count, run.updated_count) == (3, 1, 1)
    with Session() as s:
        assert count(s, Question) == 3
        assert count(s, PollRun) == 2
        assert set(s.scalars(select(Question.local_status))) == {"NEW"}


def test_api_error_marks_run_failed_and_keeps_earlier_pages(Session, caplog):
    client = FakeClient([[item(1)], [item(2)], [item(3)]], fail_at=1)
    run = poll_job(client, Session)

    with Session() as s:
        stored = s.get(PollRun, run.id)
        assert stored.status == "FAILED"
        assert stored.error_message == "SERVER_ERROR: Trendyol server error."
        assert (stored.fetched_count, stored.inserted_count, stored.error_count) == (1, 1, 1)
        assert stored.finished_at is not None and stored.duration_ms is not None
        assert count(s, Question) == 1  # page 0 was committed before the failure
    assert "SERVER_ERROR" in caplog.text


def test_unexpected_exception_is_recorded_not_raised(Session):
    client = FakeClient([[item(1)]], fail_at=0, error=KeyError("totalPages"))
    run = poll_job(client, Session)
    assert run.status == "FAILED"
    assert run.error_message.startswith("KeyError")


def test_empty_result_is_success(Session):
    run = poll_job(FakeClient([], total_pages=0), Session)
    assert (run.status, run.fetched_count, run.error_message) == ("SUCCESS", 0, None)


def test_empty_page_stops_even_if_total_pages_lies(Session):
    client = FakeClient([[item(1)]], total_pages=99)
    run = poll_job(client, Session)
    assert client.calls == [(0, 50), (1, 50)]  # page 1 came back empty -> stop
    assert run.status == "SUCCESS"


def test_page_limit_guards_against_endless_pagination(Session, monkeypatch):
    monkeypatch.setattr(sched_mod, "MAX_PAGES", 3)
    pages = [[item(i)] for i in range(10)]
    client = FakeClient(pages, total_pages=10**6)
    run = poll_job(client, Session)
    assert len(client.calls) == 3
    assert run.status == "FAILED" and "Page limit" in run.error_message


def test_poll_job_survives_db_failure(caplog):
    def broken_session_factory():
        raise RuntimeError("db down")

    assert poll_job(FakeClient([[item(1)]]), broken_session_factory) is None
    assert "Poll job crashed" in caplog.text


@pytest.fixture
def clean_scheduler():
    stop_scheduler()
    yield
    stop_scheduler()


def test_scheduler_registers_job_with_interval(clean_scheduler):
    scheduler = start_scheduler(FakeClient([]), interval_seconds=42, paused=True)
    job = scheduler.get_job(POLL_JOB_ID)
    assert job.trigger.interval == timedelta(seconds=42)
    assert job.max_instances == 1 and job.coalesce is True
    assert job.func is poll_job


def test_scheduler_started_only_once_per_process(clean_scheduler, caplog):
    caplog.set_level(logging.INFO)
    first = start_scheduler(FakeClient([]), interval_seconds=60, paused=True)
    second = start_scheduler(FakeClient([]), interval_seconds=5, paused=True)
    assert first is second
    assert len(first.get_jobs()) == 1
    assert first.get_job(POLL_JOB_ID).trigger.interval == timedelta(seconds=60)
    assert "already running" in caplog.text


def test_scheduler_default_and_invalid_interval(clean_scheduler):
    with pytest.raises(ConfigError):
        start_scheduler(FakeClient([]), interval_seconds=0, paused=True)
    scheduler = start_scheduler(FakeClient([]), interval_seconds=None, paused=True)
    assert scheduler.get_job(POLL_JOB_ID).trigger.interval == timedelta(seconds=300)
