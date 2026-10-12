import pytest
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import crud
from app.models import PollRun, Question


def q(tid, status="WAITING_FOR_ANSWER", text="Beden tablosu var mı?", **extra):
    payload = {"id": tid, "status": status, "text": text, **extra}
    return {
        "trendyol_question_id": tid,
        "supplier_id": 111,
        "product_name": "Tişört",
        "question_text": text,
        "trendyol_status": status,
        "question_created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "raw_payload": payload,
    }


def get(session, tid):
    session.expire_all()
    return session.scalars(select(Question).where(Question.trendyol_question_id == tid)).one()


def test_upsert_inserts_then_is_idempotent(session):
    assert crud.upsert_questions(session, [q(1), q(2, status="ANSWERED")]) == {"inserted": 2, "updated": 0}
    assert get(session, 1).local_status == "NEW"
    assert get(session, 2).local_status == "CLOSED"

    # Same payload again: no duplicate row, no update.
    assert crud.upsert_questions(session, [q(1), q(2, status="ANSWERED")]) == {"inserted": 0, "updated": 0}
    assert session.query(Question).count() == 2


def test_upsert_dedupes_within_batch(session):
    result = crud.upsert_questions(session, [q(1, text="eski"), q(1, text="yeni")])
    assert result == {"inserted": 1, "updated": 0}
    assert get(session, 1).question_text == "yeni"


def test_upsert_updates_source_fields_but_keeps_local_fields(session):
    crud.upsert_questions(session, [q(1)])
    row = get(session, 1)
    assert crud.try_lock_for_answering(session, row.id)
    crud.mark_answered(session, row.id, "Evet, var.", answer_id=99)

    # Trendyol confirms: text changed, status ANSWERED.
    assert crud.upsert_questions(session, [q(1, status="ANSWERED", text="Beden tablosu?")])["updated"] == 1
    row = get(session, 1)
    assert row.question_text == "Beden tablosu?"
    assert row.local_status == "ANSWERED"
    assert (row.answer_text, row.answer_id) == ("Evet, var.", 99)
    assert row.answered_at is not None


def test_upsert_does_not_touch_answering(session):
    crud.upsert_questions(session, [q(1)])
    row = get(session, 1)
    crud.try_lock_for_answering(session, row.id)
    crud.upsert_questions(session, [q(1, status="ANSWERED")])
    assert get(session, 1).local_status == "ANSWERING"


def test_upsert_closes_question_answered_elsewhere(session):
    crud.upsert_questions(session, [q(1)])
    crud.upsert_questions(session, [q(1, status="ANSWERED")])
    assert get(session, 1).local_status == "CLOSED"


def test_upsert_reopens_when_trendyol_reopens(session):
    crud.upsert_questions(session, [q(1, status="ANSWERED")])
    assert get(session, 1).local_status == "CLOSED"
    crud.upsert_questions(session, [q(1, status="WAITING_FOR_ANSWER")])
    assert get(session, 1).local_status == "REOPENED"


def test_upsert_rejected_is_not_answerable(session):
    crud.upsert_questions(session, [q(1, status="REJECTED")])
    assert get(session, 1).local_status == "CLOSED"


def test_upsert_reopens_failed_while_still_waiting_and_keeps_history(session):
    crud.upsert_questions(session, [q(1)])
    row = get(session, 1)
    crud.try_lock_for_answering(session, row.id)
    crud.mark_failed(session, row.id, "TIMEOUT", "timed out", retryable=True)
    crud.upsert_questions(session, [q(1)])  # identical payload, still WAITING_FOR_ANSWER
    row = get(session, 1)
    # retry_count counts retries started, not failures: still 0 here.
    assert (row.local_status, row.retry_count, row.error_code) == ("REOPENED", 0, "TIMEOUT")


def test_upsert_reopens_answered_while_still_waiting_then_stays_reopened(session):
    crud.upsert_questions(session, [q(1)])
    row_id = get(session, 1).id
    crud.try_lock_for_answering(session, row_id)
    crud.mark_answered(session, row_id, "Evet, var.", answer_id=5)

    assert crud.upsert_questions(session, [q(1)]) == {"inserted": 0, "updated": 1}
    row = get(session, 1)
    assert (row.local_status, row.answer_text) == ("REOPENED", "Evet, var.")

    # Next poll: REOPENED is kept (not turned into NEW) and counts as no-op.
    assert crud.upsert_questions(session, [q(1)]) == {"inserted": 0, "updated": 0}
    assert get(session, 1).local_status == "REOPENED"
    assert crud.get_new_questions(session) == []


def test_get_new_questions_oldest_first(session):
    old, new = q(1), q(2)
    old["question_created_at"] = datetime(2026, 1, 1, tzinfo=timezone.utc)
    crud.upsert_questions(session, [new, old, q(3, status="ANSWERED")])
    assert [r.trendyol_question_id for r in crud.get_new_questions(session)] == [1, 2]
    assert len(crud.get_new_questions(session, limit=1)) == 1


def test_lock_only_once(session):
    crud.upsert_questions(session, [q(1)])
    row_id = get(session, 1).id
    assert crud.try_lock_for_answering(session, row_id) is True
    assert crud.try_lock_for_answering(session, row_id) is False
    assert crud.get_new_questions(session) == []


def test_lock_concurrent_single_winner(Session):
    with Session() as s:
        crud.upsert_questions(s, [q(1)])
        row_id = get(s, 1).id

    workers = 8
    barrier = threading.Barrier(workers)
    results = []

    def worker():
        with Session() as s:
            barrier.wait()
            results.append(crud.try_lock_for_answering(s, row_id))

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and results.count(False) == workers - 1


def test_mark_transitions_require_answering(session):
    crud.upsert_questions(session, [q(1)])
    row_id = get(session, 1).id
    # Not locked yet: both must refuse.
    assert crud.mark_answered(session, row_id, "x") is False
    assert crud.mark_failed(session, row_id, "E", "e", retryable=False) is False
    assert get(session, 1).local_status == "NEW"

    crud.try_lock_for_answering(session, row_id)
    assert crud.mark_failed(session, row_id, "E", "e", retryable=False) is True
    # Already FAILED: a late mark_answered must not overwrite it.
    assert crud.mark_answered(session, row_id, "x") is False


def test_status_check_constraint(session):
    import pytest
    from sqlalchemy.exc import IntegrityError

    crud.upsert_questions(session, [q(1)])
    row = get(session, 1)
    row.local_status = "BOGUS"
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add(PollRun())
    session.commit()
    assert session.scalars(select(PollRun.status)).one() == "RUNNING"


def test_lock_accepts_reopened_but_not_failed_answered_closed(session):
    for tid, status in enumerate(["REOPENED", "FAILED", "ANSWERED", "CLOSED"], start=1):
        session.add(Question(trendyol_question_id=tid, question_text="?", local_status=status))
    session.commit()
    results = {get(session, tid).local_status: crud.try_lock_for_answering(session, get(session, tid).id)
               for tid in range(1, 5)}
    assert results == {"REOPENED": True, "FAILED": False, "ANSWERED": False, "CLOSED": False}


def test_mark_closed_only_from_answering(session):
    crud.upsert_questions(session, [q(1)])
    row_id = get(session, 1).id
    assert crud.mark_closed(session, row_id, "QUESTION_ALREADY_ANSWERED", "x") is False
    crud.try_lock_for_answering(session, row_id)
    assert crud.mark_closed(session, row_id, "QUESTION_ALREADY_ANSWERED", "x") is True
    row = get(session, 1)
    assert (row.local_status, row.retryable, row.retry_count) == ("CLOSED", False, 0)
    assert crud.get_answerable_questions(session) == []



def add_status(session, tid, status, **extra):
    session.add(Question(trendyol_question_id=tid, question_text="?", local_status=status, **extra))
    session.commit()
    return get(session, tid)


def fail_once(session, tid, code="SERVER_ERROR", retryable=True):
    crud.upsert_questions(session, [q(tid)])
    row_id = get(session, tid).id
    assert crud.try_lock_for_answering(session, row_id)
    assert crud.mark_failed(session, row_id, code, "msg", retryable=retryable)
    return row_id


def test_failed_retryable_lockable_non_retryable_not(session):
    retryable_id = fail_once(session, 1, retryable=True)
    final_id = fail_once(session, 2, code="FORBIDDEN_WORD", retryable=False)
    assert crud.try_lock_for_answering(session, retryable_id) is True
    assert crud.try_lock_for_answering(session, final_id) is False
    assert get(session, 2).local_status == "FAILED"


@pytest.mark.parametrize("status", ["ANSWERING", "ANSWERED", "CLOSED"])
def test_terminal_and_busy_states_not_lockable_even_if_retryable(session, status):
    row = add_status(session, 1, status, retryable=True)
    assert crud.try_lock_for_answering(session, row.id) is False


def test_lock_sets_locked_at_and_exits_clear_it(session):
    for tid, finish in [(1, "answered"), (2, "failed"), (3, "closed")]:
        crud.upsert_questions(session, [q(tid)])
        row_id = get(session, tid).id
        before = datetime.now(timezone.utc)
        crud.try_lock_for_answering(session, row_id)
        locked = get(session, tid).locked_at
        assert locked is not None and locked >= before - timedelta(seconds=1)
        if finish == "answered":
            crud.mark_answered(session, row_id, "Evet, var.")
        elif finish == "failed":
            crud.mark_failed(session, row_id, "SERVER_ERROR", "m", retryable=True)
        else:
            crud.mark_closed(session, row_id, "QUESTION_ALREADY_ANSWERED", "m")
        assert get(session, tid).locked_at is None


def test_retry_count_counts_retries_and_survives_success(session):
    crud.upsert_questions(session, [q(1)])
    row_id = get(session, 1).id
    crud.try_lock_for_answering(session, row_id)            # first attempt
    assert get(session, 1).retry_count == 0
    crud.mark_failed(session, row_id, "SERVER_ERROR", "m", retryable=True)
    assert get(session, 1).retry_count == 0                   # failure alone is not a retry

    crud.try_lock_for_answering(session, row_id)            # retry #1
    assert get(session, 1).retry_count == 1
    crud.mark_failed(session, row_id, "NETWORK_ERROR", "m", retryable=True)
    crud.try_lock_for_answering(session, row_id)            # retry #2
    assert get(session, 1).retry_count == 2

    crud.mark_answered(session, row_id, "Evet, var.")
    row = get(session, 1)
    assert (row.local_status, row.retry_count, row.error_code, row.retryable) == (
        "ANSWERED", 2, None, False)

    crud.upsert_questions(session, [q(99)])
    assert get(session, 99).retry_count == 0                  # brand new question


def test_reclaim_only_stale_answering(session):
    now = datetime.now(timezone.utc)
    old = add_status(session, 1, "ANSWERING", locked_at=now - timedelta(minutes=11))
    fresh = add_status(session, 2, "ANSWERING", locked_at=now - timedelta(minutes=2))
    legacy = add_status(session, 3, "ANSWERING", locked_at=None)
    other = add_status(session, 4, "FAILED", locked_at=None, retryable=False)

    ids = crud.reclaim_stale_answering(session, now - timedelta(minutes=10))
    assert sorted(ids) == sorted([old.id, legacy.id])

    row = get(session, 1)
    assert (row.local_status, row.retryable, row.error_code, row.locked_at) == (
        "FAILED", True, crud.STALE_ANSWERING_CODE, None)
    assert row.error_message == crud.STALE_ANSWERING_MESSAGE
    assert get(session, 2).local_status == "ANSWERING"
    assert get(session, 4).retryable is False and get(session, 4).local_status == "FAILED"

    # Idempotent: nothing left to reclaim.
    assert crud.reclaim_stale_answering(session, now - timedelta(minutes=10)) == []
    # Reclaimed row is retryable: lock works and counts as a retry.
    assert crud.try_lock_for_answering(session, old.id) is True
    assert get(session, 1).retry_count == 1


def test_reclaim_concurrent_runs_reclaim_once(Session):
    import threading

    with Session() as s:
        for tid in range(1, 6):
            s.add(Question(trendyol_question_id=tid, question_text="?", local_status="ANSWERING",
                           locked_at=datetime.now(timezone.utc) - timedelta(hours=1)))
        s.commit()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    barrier = threading.Barrier(4)
    results = []

    def run():
        with Session() as s:
            barrier.wait()
            results.append(crud.reclaim_stale_answering(s, cutoff))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    all_ids = [i for r in results for i in r]
    assert len(all_ids) == 5 and len(set(all_ids)) == 5   # each row reclaimed exactly once


def test_mark_after_reclaim_is_refused(session):
    crud.upsert_questions(session, [q(1)])
    row_id = get(session, 1).id
    crud.try_lock_for_answering(session, row_id)
    crud.reclaim_stale_answering(session, datetime.now(timezone.utc) + timedelta(seconds=1))
    assert crud.mark_answered(session, row_id, "Evet, var.") is False
    assert get(session, 1).local_status == "FAILED"


def test_answerable_list_includes_only_retryable_failed(session):
    add_status(session, 1, "NEW")
    add_status(session, 2, "REOPENED")
    add_status(session, 3, "FAILED", retryable=True)
    add_status(session, 4, "FAILED", retryable=False)
    add_status(session, 5, "ANSWERING", retryable=True)
    assert sorted(r.trendyol_question_id for r in crud.get_answerable_questions(session)) == [1, 2, 3]


def test_upsert_stores_full_raw_payload_roundtrip(session):
    from app.trendyol_client import TrendyolClient
    from tests.test_trendyol_client import FULL_QUESTION

    item = TrendyolClient(111, "k", "s", "https://apigw.example")._map_question(FULL_QUESTION)
    assert crud.upsert_questions(session, [item]) == {"inserted": 1, "updated": 0}
    assert get(session, 900).raw_payload == FULL_QUESTION  # incl. nested answer objects
