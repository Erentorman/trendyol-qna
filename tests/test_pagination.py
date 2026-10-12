"""Pagination end to end: real TrendyolClient (fake HTTP transport) -> poll_job -> DB.

Covers the acceptance criteria for fetching ALL waiting questions without losing any.
No real Trendyol traffic: replies come from the FakeAdapter queue in page order.
"""
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select

from app.models import PollRun, Question
from app.scheduler import poll_job
from tests.test_trendyol_client import make_client


def raw(tid, status="WAITING_FOR_ANSWER", text="Kargo ne zaman?", **extra):
    return {"id": tid, "text": text, "status": status, "userName": "A***",
            "productName": "Tişört", "productMainId": "PM", "creationDate": 1758000000000 + tid,
            **extra}


def page(items, number, total_pages, total_elements=None, size=2):
    return {"content": items, "page": number, "size": size, "totalPages": total_pages,
            "totalElements": len(items) if total_elements is None else total_elements}


def pages_requested(adapter):
    return [parse_qs(urlparse(req.url).query)["page"][0] for req, _ in adapter.calls]


def rows(Session):
    with Session() as s:
        return {q.trendyol_question_id: q for q in s.scalars(select(Question))}


def test_single_page(Session):
    client, adapter, _ = make_client((200, page([raw(1), raw(2)], 0, 1)))
    run = poll_job(client, Session, page_size=2)
    assert pages_requested(adapter) == ["0"]
    assert (run.status, run.fetched_count, run.inserted_count) == ("SUCCESS", 2, 2)


def test_three_pages_all_fetched_with_distinct_questions(Session):
    client, adapter, _ = make_client(
        (200, page([raw(1), raw(2)], 0, 3, 5)),
        (200, page([raw(3), raw(4)], 1, 3, 5)),
        (200, page([raw(5)], 2, 3, 5)),
    )
    run = poll_job(client, Session, page_size=2)
    assert pages_requested(adapter) == ["0", "1", "2"]  # starts at 0, no 4th request
    assert (run.status, run.fetched_count, run.inserted_count) == ("SUCCESS", 5, 5)
    assert set(rows(Session)) == {1, 2, 3, 4, 5}


def test_empty_last_page_ends_poll_successfully(Session):
    client, adapter, _ = make_client(
        (200, page([raw(1), raw(2)], 0, 2, 2)),
        (200, page([], 1, 2, 2)),
    )
    run = poll_job(client, Session, page_size=2)
    assert pages_requested(adapter) == ["0", "1"]
    assert (run.status, run.fetched_count) == ("SUCCESS", 2)


def test_zero_total_pages_and_empty_content_are_safe(Session):
    client, adapter, _ = make_client((200, page([], 0, 0, 0)))
    run = poll_job(client, Session)
    assert pages_requested(adapter) == ["0"]
    assert (run.status, run.fetched_count, run.error_message) == ("SUCCESS", 0, None)


def test_same_id_on_two_pages_creates_one_row_last_wins(Session):
    client, _, _ = make_client(
        (200, page([raw(1), raw(2)], 0, 2)),
        (200, page([raw(2, text="Yeni metin"), raw(3)], 1, 2)),
    )
    run = poll_job(client, Session, page_size=2)
    stored = rows(Session)
    assert set(stored) == {1, 2, 3}  # one row per question id
    assert run.status == "SUCCESS"
    # Row 2 was inserted on page 0 and refreshed on page 1.
    assert stored[2].question_text == "Yeni metin"
    assert stored[2].raw_payload == raw(2, text="Yeni metin")


def test_changed_question_on_next_poll_updates_fields_and_raw_payload(Session):
    first = raw(1, text="Eski", reason=None)
    client, _, _ = make_client((200, page([first], 0, 1)))
    poll_job(client, Session)

    changed = raw(1, status="ANSWERED", text="Yeni", answer={"id": 9, "text": "Evet",
                                                              "hasPrivateInfo": False})
    client, _, _ = make_client((200, page([changed], 0, 1)))
    run = poll_job(client, Session)

    row = rows(Session)[1]
    assert (run.inserted_count, run.updated_count) == (0, 1)
    assert (row.question_text, row.trendyol_status) == ("Yeni", "ANSWERED")
    assert row.raw_payload == changed  # replaced by the latest full item
    with Session() as s:
        assert s.scalar(select(func.count()).select_from(Question)) == 1


def test_raw_payload_keeps_full_item_including_unknown_fields(Session):
    item = raw(7, customerId=55, showUserName=False, public=True, imageUrl="i", webUrl="w",
               rejectedAnswer={"id": 1, "reason": "r"}, answeredDateMessage=None,
               futureField={"a": [1, 2]})
    client, _, _ = make_client((200, page([item], 0, 1)))
    poll_job(client, Session)
    assert rows(Session)[7].raw_payload == item


def test_error_on_second_page_fails_run_loudly_and_keeps_first_page(Session, caplog):
    boom = (503, {"message": "down"})
    client, adapter, _ = make_client(
        (200, page([raw(1), raw(2)], 0, 3, 6)),
        boom, boom, boom,  # page 1 fails on all 3 attempts
    )
    run = poll_job(client, Session, page_size=2)

    assert run.status == "FAILED"  # not reported as a complete SUCCESS
    assert run.error_message == "SERVER_ERROR: Trendyol server error."
    assert run.error_count == 1 and run.fetched_count == 2
    assert pages_requested(adapter) == ["0", "1", "1", "1"]  # page 2 never fetched
    assert set(rows(Session)) == {1, 2}  # page 0 kept, nothing pretends page 1/2 arrived
    assert "SERVER_ERROR" in caplog.text
    with Session() as s:
        assert s.scalar(select(func.count()).select_from(PollRun)) == 1


def test_failed_run_recovers_on_next_poll(Session):
    boom = (503, {"message": "down"})
    client, _, _ = make_client((200, page([raw(1), raw(2)], 0, 2, 3)), boom, boom, boom)
    assert poll_job(client, Session, page_size=2).status == "FAILED"

    client, _, _ = make_client((200, page([raw(1), raw(2)], 0, 2, 3)),
                               (200, page([raw(3)], 1, 2, 3)))
    run = poll_job(client, Session, page_size=2)
    assert (run.status, run.inserted_count) == ("SUCCESS", 1)
    assert set(rows(Session)) == {1, 2, 3}
