"""`askbot/away.py` 테스트 — 부재 모드(`/stay off|on`)의 상태 파일.

감시기와 `ask_me.py` 는 이 파일들로만 말을 주고받는다. 한쪽이 반쯤 쓴 파일을
다른 쪽이 읽거나, 죽은 감시기의 잠금이 새 감시기를 영영 막거나, 만료된 질문이
다음 글을 답으로 가로채면 부재 중 대화가 조용히 망가진다.
"""

import datetime as dt
import json

from askbot import away

KST = dt.timezone(dt.timedelta(hours=9))
T0 = dt.datetime(2026, 9, 30, 20, 0, tzinfo=KST)


def _p(tmp_path):
    return away.paths(tmp_path / "data")


def test_away_flag_round_trip(tmp_path):
    p = _p(tmp_path)
    assert away.is_away(p) is False

    away.set_away(p, T0)

    assert away.is_away(p) is True
    assert json.loads(p.away.read_text(encoding="utf-8")) == {"since": T0.isoformat()}


def test_clear_away_also_removes_the_lock(tmp_path):
    """끄면 감시기 잠금도 지운다 — 남은 잠금이 다음 `/stay off` 를 막지 않게."""
    p = _p(tmp_path)
    away.set_away(p, T0)
    away.write_lock(p, T0, 111)

    away.clear_away(p)

    assert not p.away.exists() and not p.lock.exists()
    away.clear_away(p)          # 이미 꺼져 있어도 조용하다


def test_set_away_also_removes_a_stale_offset(tmp_path):
    """일주일 묵은 offset 이 남아 있으면, 텔레그램이 새로 매기는 update_id 가
    그보다 작을 수 있다 — `getUpdates` 가 그 offset 미만을 전부 걸러내
    새 메시지가 조용히 사라진다. 새 부재를 시작할 때는 지운다."""
    p = _p(tmp_path)
    away.save_offset(p, 999999)

    away.set_away(p, T0)

    assert away.load_offset(p) is None


def test_clear_away_also_removes_a_pending_question(tmp_path):
    """끌 때 걸린 질문을 남겨 두면, 다음 부재의 첫 메시지가 이미 끝난
    질문의 답으로 잡힌다."""
    p = _p(tmp_path)
    away.set_away(p, T0)
    away.write_pending(p, qid="q1", options=[],
                       expires_at=T0 + dt.timedelta(minutes=5))

    away.clear_away(p)

    assert not p.pending.exists()


def test_lock_is_fresh_within_90_seconds_only(tmp_path):
    p = _p(tmp_path)
    assert away.lock_is_fresh(p, T0) is False           # 잠금 없음

    away.write_lock(p, T0, 111)

    assert away.lock_is_fresh(p, T0 + dt.timedelta(seconds=90)) is True
    assert away.lock_is_fresh(p, T0 + dt.timedelta(seconds=91)) is False


def test_a_broken_lock_is_not_fresh(tmp_path):
    p = _p(tmp_path)
    p.root.mkdir(parents=True)
    p.lock.write_text("{반쯤", encoding="utf-8")

    assert away.lock_is_fresh(p, T0) is False


def test_release_lock_only_removes_our_own(tmp_path):
    """다른 감시기가 `--takeover` 로 가져간 잠금을 옛 감시기가 지우면 안 된다."""
    p = _p(tmp_path)
    away.write_lock(p, T0, 222)

    away.release_lock(p, 111)
    assert p.lock.exists()

    away.release_lock(p, 222)
    assert not p.lock.exists()


def test_offset_round_trip(tmp_path):
    p = _p(tmp_path)
    assert away.load_offset(p) is None

    away.save_offset(p, 501)

    assert away.load_offset(p) == 501


def test_pending_question_round_trip_and_expiry(tmp_path):
    p = _p(tmp_path)
    away.write_pending(p, qid="q1", options=["지금", "내일"],
                       expires_at=T0 + dt.timedelta(minutes=5))

    got = away.read_pending(p, T0)
    assert got["qid"] == "q1" and got["options"] == ["지금", "내일"]

    # 만료되면 없는 것으로 보고 **지운다** — 다음 글을 답으로 가로채지 않게.
    assert away.read_pending(p, T0 + dt.timedelta(minutes=5)) is None
    assert not p.pending.exists()


def test_pending_question_stores_asked_at_when_given(tmp_path):
    """질문을 던진 시각을 같이 적어 둔다 — 그보다 먼저 온 글을 답으로
    잡지 않으려면(`askbot/watch.py::_handle`) 필요하다."""
    p = _p(tmp_path)
    away.write_pending(p, qid="q1", options=[],
                       expires_at=T0 + dt.timedelta(minutes=5), asked_at=T0)

    got = away.read_pending(p, T0)

    assert got["asked_at"] == T0.isoformat()


def test_pending_question_without_asked_at_omits_it(tmp_path):
    p = _p(tmp_path)
    away.write_pending(p, qid="q1", options=[],
                       expires_at=T0 + dt.timedelta(minutes=5))

    got = away.read_pending(p, T0)

    assert "asked_at" not in got


def test_a_broken_pending_file_is_dropped(tmp_path):
    p = _p(tmp_path)
    p.root.mkdir(parents=True)
    p.pending.write_text('{"options": []}', encoding="utf-8")   # qid 없음

    assert away.read_pending(p, T0) is None
    assert not p.pending.exists()


def test_clear_pending_only_removes_the_named_question(tmp_path):
    p = _p(tmp_path)
    away.write_pending(p, qid="q2", options=[],
                       expires_at=T0 + dt.timedelta(minutes=5))

    away.clear_pending(p, "q1")
    assert p.pending.exists()

    away.clear_pending(p, "q2")
    assert not p.pending.exists()


def test_answer_is_taken_once(tmp_path):
    p = _p(tmp_path)
    assert away.take_answer(p, "q1") is None

    away.write_answer(p, qid="q1", raw="2", answer="내일")

    assert away.take_answer(p, "q1") == {"qid": "q1", "raw": "2", "answer": "내일"}
    assert away.take_answer(p, "q1") is None


def test_new_qid_is_unique_and_filename_safe():
    ids = {away.new_qid() for _ in range(50)}
    assert len(ids) == 50
    assert all(q.isalnum() for q in ids)


def test_requests_are_saved_and_taken_once(tmp_path):
    p = _p(tmp_path)
    away.save_request(p, "배포해줘")
    away.save_request(p, "손절선 바꿔줘")

    assert away.take_requests(p) == ["배포해줘", "손절선 바꿔줘"]
    assert away.take_requests(p) == []


def test_writes_leave_no_temp_files(tmp_path):
    p = _p(tmp_path)
    away.set_away(p, T0)
    away.write_lock(p, T0, 1)
    away.save_offset(p, 3)
    away.write_pending(p, qid="q", options=[], expires_at=T0)
    away.write_answer(p, qid="q", raw="a", answer="a")

    assert not list(p.root.glob("*.tmp"))


def test_a_briefly_locked_file_is_still_deleted(tmp_path, monkeypatch):
    """Windows 는 다른 프로세스가 연 파일의 삭제를 거부한다(WinError 32).
    감시기와 ask_me.py 는 별개 프로세스라 겹칠 수 있다 — 잠깐 뒤 다시 한다."""
    p = _p(tmp_path)
    away.write_answer(p, qid="q1", raw="1", answer="지금")
    real_unlink = type(p.root).unlink
    failures = {"left": 2}

    def flaky_unlink(self, missing_ok=False):
        if failures["left"]:
            failures["left"] -= 1
            raise PermissionError("[WinError 32] 다른 프로세스가 사용 중")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(type(p.root), "unlink", flaky_unlink)
    monkeypatch.setattr(away.time, "sleep", lambda s: None)

    assert away.take_answer(p, "q1")["answer"] == "지금"
    assert not p.answer("q1").exists()
    assert failures["left"] == 0
