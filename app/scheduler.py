"""Periodic polling of Trendyol questions into the DB.

Assumes ONE process runs the scheduler (dev/demo). With several web workers, run the
poller as a separate process or guard it with a DB advisory lock (see README).
"""

import atexit
import logging
import threading
import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app import crud
from app.config import ConfigError
from app.db import SessionLocal
from app.models import PollRun
from app.trendyol_client import MAX_PAGE_SIZE, TrendyolError

logger = logging.getLogger(__name__)

POLL_JOB_ID = "poll_questions"
DEFAULT_POLL_INTERVAL_SECONDS = 300
# Hard stop against a misbehaving totalPages: 200 * 50 = 10,000 waiting questions.
MAX_PAGES = 200

_scheduler = None
_scheduler_lock = threading.Lock()


def poll_job(client, session_factory=SessionLocal, page_size=MAX_PAGE_SIZE):
    """One poll: walk all WAITING_FOR_ANSWER pages, upsert, record a PollRun.

    Never raises: a failing poll is logged and stored as FAILED so the next run still happens.
    """
    try:
        return _run_poll(client, session_factory, page_size)
    except Exception:
        # Reached only if even the PollRun bookkeeping failed (e.g. DB down).
        logger.exception("Poll job crashed before its PollRun could be saved")
        return None


def _run_poll(client, session_factory, page_size):
    with session_factory() as session:
        run = PollRun(status="RUNNING", started_at=datetime.now(timezone.utc))
        session.add(run)
        session.commit()  # visible as RUNNING while in progress
        run_id = run.id  # plain int: safe to log even after a rollback expires `run`
        logger.info("Poll %s started", run_id)

        started = time.monotonic()
        fetched = inserted = updated = errors = 0
        status, error_message = "SUCCESS", None
        try:
            page = 0
            while True:
                result = client.fetch_waiting_questions(page=page, size=page_size)
                questions = result["questions"]
                counts = crud.upsert_questions(session, questions)  # commits per page
                fetched += len(questions)
                inserted += counts["inserted"]
                updated += counts["updated"]
                logger.debug("Poll %s page %s: %s questions", run_id, page, len(questions))

                page += 1
                # totalPages is re-read every page, so a shrinking/growing list is followed.
                if not questions or page >= result["total_pages"]:
                    break
                if page >= MAX_PAGES:
                    raise RuntimeError(f"Page limit {MAX_PAGES} reached; stopping poll")
        except TrendyolError as exc:
            session.rollback()
            errors += 1
            status, error_message = "FAILED", f"{exc.code}: {exc.message}"
            logger.error("Poll %s failed: %s (status_code=%s)", run_id, exc.code, exc.status_code)
        except Exception as exc:
            session.rollback()
            errors += 1
            status, error_message = "FAILED", f"{type(exc).__name__}: {exc}"[:1000]
            logger.exception("Poll %s failed unexpectedly", run_id)

        run.status = status
        run.error_message = error_message
        run.fetched_count, run.inserted_count, run.updated_count = fetched, inserted, updated
        run.error_count = errors
        run.finished_at = datetime.now(timezone.utc)
        run.duration_ms = int((time.monotonic() - started) * 1000)
        session.commit()

        logger.info(
            "Poll %s finished: status=%s fetched=%s inserted=%s updated=%s duration_ms=%s",
            run_id, status, fetched, inserted, updated, run.duration_ms,
        )
        return run


def start_scheduler(client, interval_seconds=None, session_factory=SessionLocal, paused=False):
    """Start the process-wide BackgroundScheduler once; later calls return the same one."""
    global _scheduler
    interval = DEFAULT_POLL_INTERVAL_SECONDS if interval_seconds is None else interval_seconds
    if interval < 1:
        raise ConfigError("POLL_INTERVAL_SECONDS must be >= 1")

    with _scheduler_lock:
        if _scheduler is not None:
            logger.info("Scheduler already running; not starting another one")
            return _scheduler

        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(
            poll_job,
            "interval",
            seconds=interval,
            id=POLL_JOB_ID,
            args=[client, session_factory],
            max_instances=1,  # a slow poll is never overlapped by the next one
            coalesce=True,  # missed runs collapse into one instead of a burst
            next_run_time=datetime.now(timezone.utc),  # first poll right away
        )
        scheduler.start(paused=paused)
        atexit.register(stop_scheduler)
        _scheduler = scheduler
        logger.info("Scheduler started: poll every %ss", interval)
        return scheduler


def stop_scheduler():
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None
