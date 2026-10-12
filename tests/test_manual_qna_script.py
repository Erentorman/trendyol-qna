import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "manual_qna_test", Path(__file__).resolve().parent.parent / "scripts" / "manual_qna_test.py")
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

ENV = {"TY_SUPPLIER_ID": "2738", "TY_API_KEY": "k", "TY_API_SECRET": "s",
       "TY_BASE_URL": "https://stageapigw.trendyol.com"}
QUESTION = {"id": 400081585, "status": "UNANSWERED", "text": "Beden tablosu var mı?",
            "creationDate": 1760000000000}


def test_production_refused():
    with pytest.raises(SystemExit):
        script.load_settings({**ENV, "TY_BASE_URL": "https://apigw.trendyol.com"})


def test_stage_accepted_and_missing_rejected():
    assert script.load_settings(ENV)["seller_id"] == "2738"
    with pytest.raises(SystemExit):
        script.load_settings({**ENV, "TY_API_KEY": ""})


def test_status_is_unanswered():
    assert script.STATUS == "UNANSWERED"


def _patch(monkeypatch, questions):
    posts = []
    monkeypatch.setattr(script, "fetch_unanswered", lambda s: (200, questions))
    monkeypatch.setattr(script, "post_answer", lambda s, qid, t: posts.append((qid, t)) or (200, 9))
    return posts


def _run(answers):
    it = iter(answers)
    return script.run(script.load_settings(ENV), input_fn=lambda _p: next(it))


def test_no_questions(monkeypatch, capsys):
    _patch(monkeypatch, [])
    assert _run([]) == 0
    assert "bulunamadı" in capsys.readouterr().out


@pytest.mark.parametrize("confirm", ["", "n", "N", "yes", "evet"])
def test_only_y_posts(monkeypatch, confirm):
    posts = _patch(monkeypatch, [QUESTION])
    assert _run(["Yeterince uzun cevap", confirm]) == 0
    assert posts == []


def test_short_answer_not_sent(monkeypatch):
    posts = _patch(monkeypatch, [QUESTION])
    assert _run(["kısa"]) == 1 and posts == []


@pytest.mark.parametrize("confirm", ["y", "Y"])
def test_success(monkeypatch, capsys, confirm):
    posts = _patch(monkeypatch, [QUESTION])
    assert _run(["Yeterince uzun cevap", confirm]) == 0
    assert posts == [(400081585, "Yeterince uzun cevap")]
    out = capsys.readouterr().out
    assert "CEVAP BAŞARILI" in out and "Answer ID: 9" in out


def test_failure_output(monkeypatch, capsys):
    _patch(monkeypatch, [QUESTION])

    class Resp:
        status_code = 400
        headers = {"X-Correlation-Id": "abc"}
        text = ""

        def json(self):
            return {"message": "yasaklı kelime"}

    def boom(s, qid, t):
        raise script.ApiError(Resp())
    monkeypatch.setattr(script, "post_answer", boom)
    assert _run(["Yeterince uzun cevap", "y"]) == 1
    out = capsys.readouterr().out
    assert "CEVAP BAŞARISIZ" in out and "400" in out and "yasaklı" in out and "abc" in out


def test_date_window_under_two_weeks():
    start, end = script.date_window()
    assert 0 < end - start < 14 * 24 * 3600 * 1000


ANSWERED_Q = {**QUESTION, "id": 1, "answer": {"id": 2032355, "text": "cevap"}}


@pytest.mark.parametrize("answer", [None, {}, {"id": None, "text": ""}, "garip"])
def test_empty_answer_counts_as_unanswered(answer):
    assert script.has_answer({**QUESTION, "answer": answer}) is False
    assert script.has_answer(QUESTION) is False  # no answer key at all


def test_filled_answer_counts_as_answered():
    assert script.has_answer(ANSWERED_Q) is True


@pytest.mark.parametrize("answer", [None, {}])
def test_unanswered_variants_are_selected(monkeypatch, capsys, answer):
    posts = _patch(monkeypatch, [{**QUESTION, "answer": answer}])
    assert _run(["Yeterince uzun cevap", "y"]) == 0
    assert posts == [(400081585, "Yeterince uzun cevap")]


def test_first_answered_second_unanswered(monkeypatch, capsys):
    posts = _patch(monkeypatch, [ANSWERED_Q, QUESTION])
    assert _run(["Yeterince uzun cevap", "y"]) == 0
    out = capsys.readouterr().out
    assert "Atlandı: Question ID 1 zaten cevaplanmış." in out
    assert "2032355" not in out.split("TRENDYOL STAGE QNA")[0]
    assert posts[0][0] == 400081585


def test_all_answered_exits_cleanly_without_prompt(monkeypatch, capsys):
    posts = _patch(monkeypatch, [ANSWERED_Q, {**ANSWERED_Q, "id": 2}])
    assert _run([]) == 0  # input_fn would raise StopIteration if prompted
    out = capsys.readouterr().out
    assert "gerçekten cevapsız soru bulunamadı" in out and posts == []
