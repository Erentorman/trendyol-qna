import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models import Question
from tests.test_routes import FakeTrendyol  # noqa: F401  (fixtures below reuse make_app)
from tests.test_routes import make_app  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


def add(Session, **fields):
    defaults = dict(trendyol_question_id=1, question_text="Pamuklu mu?", local_status="NEW",
                    trendyol_status="WAITING_FOR_ANSWER", product_name="Basic Tişört",
                    customer_name="A*** K***",
                    question_created_at=datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc))
    with Session() as s:
        q = Question(**{**defaults, **fields})
        s.add(q)
        s.commit()
        return q.id


def card_html(html, qid):
    match = re.search(rf'<article class="card" data-question-id="{qid}".*?</article>', html, re.S)
    assert match, f"card {qid} not rendered"
    return match.group(0)


def get_index(make_app):
    return make_app(FakeTrendyol()).test_client().get("/").get_data(as_text=True)


def test_new_question_card_fields(Session, make_app):
    qid = add(Session)
    card = card_html(get_index(make_app), qid)
    assert "Basic Tişört" in card and "A*** K***" in card and "Pamuklu mu?" in card
    assert "01.09.2026 12:30" in card  # 09:30 UTC shown as Istanbul time
    assert 'data-status="NEW"' in card and ">NEW<" in card
    assert 'data-role="previous"' not in card


def test_reopened_card_shows_previous_answer(Session, make_app):
    qid = add(Session, local_status="REOPENED", answer_text="Evet, %100 pamuk.",
              answered_at=datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc), error_code=None)
    card = card_html(get_index(make_app), qid)
    assert "status-reopened" in card and ">REOPENED<" in card
    assert "Önceki cevap:</strong> Evet, %100 pamuk." in card
    assert "02.09.2026 10:00" in card


def test_textarea_limits_and_assets(Session, make_app):
    qid = add(Session)
    html = get_index(make_app)
    card = card_html(html, qid)
    assert 'minlength="10"' in card and 'maxlength="2000"' in card
    assert 'data-role="counter">0 / 2000<' in card
    assert 'data-role="submit" disabled' in card  # enabled by JS once text is valid
    assert 'data-min="10" data-max="2000"' in html
    assert "/static/app.js" in html and "/static/style.css" in html


def test_static_assets_served(make_app, Session):
    client = make_app(None).test_client()
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_frontend_js_unit_tests():
    result = subprocess.run(["node", "--test", str(ROOT / "tests/js/app.test.js")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
